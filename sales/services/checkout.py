"""
Sale checkout, void and return logic.

Rules
-----
* Everything for one operation runs in a single ``transaction.atomic()``.
  Failures raise ``SaleError``; the transaction is then rolled back in full
  (nothing half-saved).
* Prices and totals are computed on the server from the product record.
* Discounts need an admin/manager.
* Stock rows, the shift, return codes and sale items are locked with
  ``select_for_update`` so concurrent tills cannot overwrite each other.
* ``client_txn_id`` makes checkout idempotent: replaying the same id returns
  the sale that already exists.
* Shift totals are recomputed from Payment/Return rows after every change.
"""
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

from django.db import transaction
from django.utils import timezone

from inventory import stock
from inventory.models import Product
from payments.models import Payment
from shifts.models import Shift

from ..models import Cart, CartItem, Sale, SaleItem, Return, ReturnItem, ReturnCode
from .receipt_number_service import get_next_receipt_number, get_next_return_receipt_number

CENT = Decimal('0.01')
MANAGER_ROLES = {'admin', 'manager'}
PAYMENT_METHODS = {'cash', 'mpesa', 'mobile', 'split'}


class SaleError(Exception):
    def __init__(self, message, status=400, code=None, **extra):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.extra = extra

    def as_response_data(self):
        data = {'error': self.message}
        if self.code:
            data['error_code'] = self.code
        data.update(self.extra)
        return data


def money(value, field='amount'):
    try:
        return Decimal(str(value if value not in (None, '') else 0)).quantize(CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        raise SaleError(f'Invalid {field}')


def get_profile(user):
    profile = getattr(user, 'userprofile', None) if user and user.is_authenticated else None
    if profile is None:
        raise SaleError('User profile not found. Please contact administrator.', status=403)
    return profile


def is_manager(user):
    from users.permissions import has_manager_rights
    return has_manager_rights(user)


def _unit_price(product, sale_type):
    if sale_type == 'wholesale' and product.wholesale_price:
        return money(product.wholesale_price)
    return money(product.selling_price)


def _parse_lines(items_data):
    """[{product, quantity, discount}] -> list of (product_id, qty, discount)."""
    if not items_data:
        raise SaleError('No items provided')
    lines = []
    for raw in items_data:
        try:
            product_id = int(raw.get('product'))
            qty = int(raw.get('quantity', 1))
        except (TypeError, ValueError, AttributeError):
            raise SaleError('Each item needs a numeric product and quantity')
        if qty <= 0:
            raise SaleError('Quantity must be greater than zero')
        lines.append((product_id, qty, money(raw.get('discount', 0), 'discount')))
    return lines


# --------------------------------------------------------------------------- checkout

def checkout(*, user, data, held_cart_id=None):
    """Create a completed sale. Returns ``(sale, created, extras)``.

    ``created`` is False when ``client_txn_id`` (or the held cart) was already
    turned into a sale; the existing sale is returned unchanged.
    """
    profile = get_profile(user)
    client_txn_id = (str(data.get('client_txn_id') or '').strip() or None)
    if client_txn_id and len(client_txn_id) > 64:
        raise SaleError('client_txn_id is too long')

    with transaction.atomic():
        if client_txn_id:
            existing = Sale.objects.filter(client_txn_id=client_txn_id).first()
            if existing:
                return existing, False, _extras(existing)

        shift = Shift.objects.select_for_update().filter(cashier=profile, status='open').first()
        if not shift:
            raise SaleError('No active shift found. Please start a shift before processing sales.')

        sale_type = data.get('sale_type') or 'retail'
        if sale_type not in ('retail', 'wholesale'):
            raise SaleError('Invalid sale type')

        # --- cart
        if held_cart_id is not None:
            cart = Cart.objects.select_for_update().filter(id=held_cart_id).first()
            if not cart:
                raise SaleError('Held order not found', status=404)
            if cart.cashier_id != profile.id and not is_manager(user):
                raise SaleError('Unauthorized to complete this order', status=403)
            if cart.status == 'closed':
                done = Sale.objects.filter(cart=cart).first()
                if done:
                    return done, False, _extras(done)
            if cart.status != 'held':
                raise SaleError('Held order not found', status=404)
            lines = [(ci.product_id, int(ci.quantity), money(ci.discount)) for ci in cart.cartitem_set.all()]
            if not lines:
                raise SaleError('Held order has no items')
        else:
            lines = _parse_lines(data.get('items'))
            cart = None

        # --- customer
        customer = None
        customer_id = data.get('customer')
        if customer_id:
            from customers.models import Customer
            customer = Customer.objects.filter(id=customer_id, is_active=True).first()
            if not customer:
                raise SaleError('Customer not found or inactive')

        # --- price lines (server prices, products locked)
        products = stock.lock_products([pid for pid, _, _ in lines])
        priced = []
        for product_id, qty, discount in lines:
            product = products.get(product_id)
            if not product or not product.is_active:
                raise SaleError(f'Product {product_id} not found or inactive')
            price = _unit_price(product, sale_type)
            if discount < 0 or discount > price * qty:
                raise SaleError(f'Invalid discount for "{product.name}"')
            priced.append((product, qty, price, discount))

        order_discount = money(data.get('discount_amount', 0), 'discount_amount')
        line_discounts = sum((d for _, _, _, d in priced), Decimal('0'))
        if (order_discount > 0 or line_discounts > 0) and not is_manager(user):
            raise SaleError('Discounts require a manager or admin', status=403, code='discount_not_allowed')

        subtotal = sum((p * q for _, q, p, _ in priced), Decimal('0'))
        discount_amount = order_discount + line_discounts
        if discount_amount > subtotal:
            raise SaleError('Discount cannot exceed the sale total')
        final_amount = subtotal - discount_amount

        # --- cart rows (record server prices)
        if cart is None:
            cart = Cart.objects.create(cashier=profile, status='closed', customer=customer)
            for product, qty, price, discount in priced:
                CartItem.objects.create(cart=cart, product=product, quantity=qty, unit_price=price, discount=discount)
        else:
            for product, qty, price, discount in priced:
                cart.cartitem_set.filter(product=product).update(unit_price=price)
            cart.status = 'closed'
            if customer:
                cart.customer = customer
            cart.save()
        customer = customer or cart.customer

        # --- sale
        sale = Sale(
            cart=cart, customer=customer, shift=shift, sale_type=sale_type,
            total_amount=subtotal, tax_amount=Decimal('0'), discount_amount=discount_amount,
            final_amount=final_amount, receipt_number=get_next_receipt_number(),
            client_txn_id=client_txn_id,
        )
        sale._skip_default_payment = True
        sale.save()

        for product, qty, price, discount in priced:
            SaleItem.objects.create(sale=sale, product=product, quantity=qty, unit_price=price, discount=discount)
            try:
                stock.deduct_for_sale(product, qty, unit_price=price, receipt_number=sale.receipt_number,
                                      user=profile, customer=customer)
            except stock.InsufficientStock as e:
                raise SaleError(str(e), code='insufficient_stock')

        # --- return code credit
        remaining_code = None
        credit = Decimal('0')
        rc_data = data.get('return_code')
        code_value = (rc_data.get('code') if isinstance(rc_data, dict) else rc_data) or ''
        code_value = str(code_value).strip().upper()
        if code_value:
            rc = ReturnCode.objects.select_for_update().filter(code=code_value).first()
            if not rc:
                raise SaleError('Return code not found', code='invalid_return_code')
            if rc.status != 'active':
                raise SaleError(f'Return code is {rc.status}', code='invalid_return_code')
            credit = min(money(rc.refund_amount), final_amount)
            rc.status = 'used'
            rc.used_at = timezone.now()
            rc.used_in_sale = sale
            rc.save()
            leftover = money(rc.refund_amount) - credit
            if leftover > 0:
                remaining_code = ReturnCode.objects.create(
                    code=ReturnCode.generate_code(leftover, rc.original_receipt_number),
                    return_record=rc.return_record, refund_amount=leftover,
                    original_receipt_number=rc.original_receipt_number, issued_from_sale=sale,
                )
            sale.return_code_used = rc.code
            sale.return_code_amount = credit
            sale.save(update_fields=['return_code_used', 'return_code_amount'])

        # --- payments
        amount_due = final_amount - credit
        _record_payments(sale, data, amount_due)

        shift.recompute_totals()
        extras = _extras(sale)
        if remaining_code:
            extras['remaining_return_code'] = {'code': remaining_code.code,
                                               'refund_amount': str(remaining_code.refund_amount)}
        return sale, True, extras


def _record_payments(sale, data, amount_due):
    method = str(data.get('payment_method') or '').strip().lower()
    if amount_due <= 0:
        return []  # fully covered by return-code credit
    if method not in PAYMENT_METHODS:
        raise SaleError('Payment method is required (cash, mpesa or split)')
    mpesa_number = data.get('mpesa_number') or ''
    reference = data.get('reference_number') or data.get('transaction_id') or None

    if method == 'split':
        split = data.get('split_data') or {}
        cash = money(split.get('cash', 0), 'cash amount')
        mpesa = money(split.get('mpesa', 0), 'M-Pesa amount')
        if cash < 0 or mpesa < 0:
            raise SaleError('Payment amounts cannot be negative')
        if abs((cash + mpesa) - amount_due) > CENT:
            raise SaleError(f'Split payments must add up to {amount_due}', code='amount_mismatch',
                            expected_amount_due=str(amount_due))
        parts = [('cash', cash), ('mpesa', mpesa)]
    else:
        parts = [('mpesa' if method in ('mpesa', 'mobile') else 'cash', amount_due)]

    payments = []
    for ptype, amount in parts:
        if amount <= 0:
            continue
        payments.append(Payment.objects.create(
            sale=sale, customer=sale.customer, payment_type=ptype, amount=amount, status='completed',
            mpesa_number=mpesa_number if ptype == 'mpesa' else '',
            reference_number=reference if ptype == 'mpesa' else None,
        ))
    return payments


def _extras(sale):
    return {'amount_due': str(money(sale.final_amount) - money(sale.return_code_amount))}


# --------------------------------------------------------------------------- void

def void_sale(*, user, sale_id, reason, require_manager=True):
    """Void a whole sale: restock, refund payments, restore return-code credit."""
    profile = get_profile(user)
    reason = (reason or '').strip()
    if not reason:
        raise SaleError('Void reason is required')

    with transaction.atomic():
        sale = Sale.objects.select_for_update().filter(id=sale_id).first()
        if not sale:
            raise SaleError('Sale not found', status=404)
        manager = is_manager(user)
        if require_manager and not manager:
            raise SaleError('Admin or Manager role required for this action', status=403)
        if not manager:
            # cashiers may only void their own sales in their current open shift
            if not (sale.shift and sale.shift.cashier_id == profile.id and sale.shift.status == 'open'):
                raise SaleError('You can only void your own sales during your open shift', status=403)
        if sale.voided:
            raise SaleError('Sale is already voided')
        if Return.objects.filter(sale=sale).exists():
            raise SaleError('This sale has returns. Process a return for the remaining items instead of voiding.')

        # return-code credit: cancel any remainder code, give the original credit back
        for issued in ReturnCode.objects.select_for_update().filter(issued_from_sale=sale):
            if issued.status == 'used':
                raise SaleError(f'Cannot void: remainder return code {issued.code} has already been used')
            issued.status = 'cancelled'
            issued.save(update_fields=['status'])
        for used in ReturnCode.objects.select_for_update().filter(used_in_sale=sale):
            used.status = 'active'
            used.used_at = None
            used.used_in_sale = None
            used.save(update_fields=['status', 'used_at', 'used_in_sale'])

        items = list(sale.saleitem_set.all())
        products = stock.lock_products([i.product_id for i in items])
        for item in items:
            qty = item.quantity - item.returned_quantity
            if qty > 0:
                stock.restore_from_sale(products[item.product_id], qty, receipt_number=sale.receipt_number,
                                        user=profile, reason=f'Sale void {sale.receipt_number} - {reason}'[:200])

        sale.payment_set.filter(status='completed').update(status='refunded')
        sale.voided = True
        sale.void_reason = reason
        sale.voided_at = timezone.now()
        sale.voided_by = profile
        sale.save(update_fields=['voided', 'void_reason', 'voided_at', 'voided_by'])

        if sale.shift_id:
            Shift.objects.select_for_update().get(pk=sale.shift_id).recompute_totals()
        return sale


# --------------------------------------------------------------------------- returns

def create_return(*, user, data):
    """Return items from a sale. Refund is issued as a return code (store credit)
    unless ``refund_method`` is cash/mpesa."""
    profile = get_profile(user)
    refund_method = data.get('refund_method') or 'return_code'
    if refund_method not in dict(Return.REFUND_METHODS):
        raise SaleError('Invalid refund method')

    with transaction.atomic():
        sale = Sale.objects.select_for_update().filter(id=data.get('original_sale_id')).first()
        if not sale:
            raise SaleError('Sale not found', status=404)
        if sale.voided:
            raise SaleError('Cannot return items from a voided sale')

        shift = Shift.objects.select_for_update().filter(cashier=profile, status='open').first()
        if not shift and not (user.is_staff or is_manager(user)):
            raise SaleError('No active shift found. Please open a shift first.')
        if refund_method == 'cash' and not shift:
            raise SaleError('Cash refunds need an open shift (they come out of the drawer)')

        items_data = data.get('items') or []
        if not items_data:
            raise SaleError('No items to return')
        requested = {}
        reasons = {}
        for raw in items_data:
            try:
                sid = int(raw.get('sale_item_id'))
                qty = int(raw.get('quantity', 0))
            except (TypeError, ValueError, AttributeError):
                raise SaleError('sale_item_id and quantity are required for each item')
            if qty <= 0:
                raise SaleError('Quantity must be greater than 0')
            requested[sid] = requested.get(sid, 0) + qty   # duplicates are summed, then validated
            reasons.setdefault(sid, (raw.get('reason') or '').strip())

        sale_items = {si.id: si for si in SaleItem.objects.select_for_update()
                      .select_related('product').filter(sale=sale, id__in=requested.keys())}
        missing = set(requested) - set(sale_items)
        if missing:
            raise SaleError(f'Sale item(s) {sorted(missing)} do not belong to this sale', status=404)

        # refund at the price actually paid: line discount spread per unit, order discount pro-rata
        line_discounts = sum((si.discount for si in sale.saleitem_set.all()), Decimal('0'))
        order_discount = max(Decimal('0'), money(sale.discount_amount) - line_discounts)
        gross = money(sale.total_amount) - line_discounts
        order_ratio = (order_discount / gross) if gross > 0 else Decimal('0')

        lines = []
        total_refund = Decimal('0')
        for sid, qty in requested.items():
            si = sale_items[sid]
            if qty > si.quantity - si.returned_quantity:
                raise SaleError(
                    f'Cannot return {qty} of "{si.product.name}". Only {si.quantity - si.returned_quantity} '
                    f'available for return (already returned: {si.returned_quantity}).')
            unit_net = money(si.unit_price) - (money(si.discount) / si.quantity)
            refund = money(unit_net * qty * (1 - order_ratio))
            lines.append((si, qty, refund))
            total_refund += refund

        ret = Return.objects.create(
            sale=sale, shift=shift, return_type=data.get('return_type') or 'partial_return',
            reason=', '.join(r for r in reasons.values() if r) or 'Returned',
            total_refund_amount=total_refund, receipt_number=get_next_return_receipt_number(),
            processed_by=profile, refund_method=refund_method,
        )
        products = stock.lock_products([si.product_id for si, _, _ in lines])
        for si, qty, refund in lines:
            ReturnItem.objects.create(return_record=ret, sale_item=si, quantity=qty,
                                      reason=reasons.get(si.id) or 'Returned', refund_amount=refund)
            si.returned_quantity += qty
            si.save(update_fields=['returned_quantity'])
            stock.restore_from_sale(products[si.product_id], qty, receipt_number=sale.receipt_number,
                                    user=profile, reason=f'Return {ret.receipt_number}')

        code = None
        if refund_method == 'return_code' and total_refund > 0:
            code = ReturnCode.objects.create(
                code=ReturnCode.generate_code(total_refund, sale.receipt_number), return_record=ret,
                refund_amount=total_refund, original_receipt_number=sale.receipt_number,
            )
        if shift:
            shift.recompute_totals()
        return ret, code
