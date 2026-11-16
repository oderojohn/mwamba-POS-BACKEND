"""
Single entry point for every stock change.

Model of stock
--------------
* ``Product.stock_quantity`` is the authoritative on-hand quantity.
* Received batches (``Batch.status == 'received'``) attribute part of that
  quantity to a batch (for FIFO, expiry and cost-of-goods).  Products created
  before batch tracking may also hold *unbatched* stock:
      unbatched = stock_quantity - sum(received batch quantities)
* Sellable stock = non-expired received batches + unbatched stock.

Every function here must be called inside ``transaction.atomic()``; rows are
locked with ``select_for_update`` so concurrent tills cannot overwrite each
other's changes.  Every change writes a ``StockMovement`` row.
"""
from decimal import Decimal

from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from .models import Batch, Product, ProductHistory, SalesHistory, StockMovement


class InsufficientStock(ValueError):
    pass


def _require_atomic():
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError('Stock changes must run inside transaction.atomic()')


def lock_products(product_ids):
    """Lock product rows in id order (consistent order avoids deadlocks)."""
    _require_atomic()
    ids = sorted({int(pid) for pid in product_ids})
    products = Product.objects.select_for_update().filter(id__in=ids).order_by('id')
    return {p.id: p for p in products}


def _sellable_batches(product, lock=True):
    today = timezone.now().date()
    qs = Batch.objects.filter(product=product, status='received', quantity__gt=0).filter(
        Q(expiry_date__isnull=True) | Q(expiry_date__gte=today)
    ).order_by('expiry_date', 'purchase_date', 'id')
    return qs.select_for_update() if lock else qs


def _received_batch_total(product):
    return Batch.objects.filter(product=product, status='received').aggregate(
        t=Sum('quantity'))['t'] or 0


def unbatched_quantity(product):
    return max(0, int(product.stock_quantity) - int(_received_batch_total(product)))


def available_quantity(product):
    batched = _sellable_batches(product, lock=False).aggregate(t=Sum('quantity'))['t'] or 0
    return int(batched) + unbatched_quantity(product)


def _set_product_stock(product, new_qty, user=None, reason=''):
    if new_qty < 0:
        raise InsufficientStock(f'Stock for "{product.name}" cannot go below zero')
    old_qty = int(product.stock_quantity)
    product.stock_quantity = new_qty
    # queryset.update avoids Product.save()'s extra SELECT; history row written explicitly
    # (EndOfDayStockView rebuilds past stock levels from these rows)
    Product.objects.filter(pk=product.pk).update(stock_quantity=new_qty, updated_at=timezone.now())
    if old_qty != new_qty:
        ProductHistory.objects.create(
            product=product, field_changed='stock_quantity', old_value=str(old_qty),
            new_value=str(new_qty), change_type='update', user=user, notes=(reason or '')[:500])


def _movement(product, qty, movement_type, reason, user):
    StockMovement.objects.create(
        product=product, movement_type=movement_type, quantity=qty,
        reason=(reason or '')[:200], user=user,
    )


def deduct_for_sale(product, quantity, *, unit_price, receipt_number, user, customer=None):
    """Take ``quantity`` units out of stock for a sale (FIFO by expiry).

    ``product`` must already be locked via ``lock_products``.
    """
    _require_atomic()
    quantity = int(quantity)
    if quantity <= 0:
        raise ValueError(f'Invalid quantity for "{product.name}"')

    batches = list(_sellable_batches(product))
    batched_available = sum(b.quantity for b in batches)
    unbatched = unbatched_quantity(product)
    if batched_available + unbatched < quantity:
        raise InsufficientStock(
            f'Insufficient stock for "{product.name}". '
            f'Available: {batched_available + unbatched}, Requested: {quantity}')

    unit_price = Decimal(str(unit_price))
    remaining = quantity
    for batch in batches:
        if remaining <= 0:
            break
        take = min(remaining, batch.quantity)
        batch.quantity -= take
        batch.save(update_fields=['quantity'])
        _movement(product, -take, 'out', f'Sale {receipt_number} - Batch {batch.batch_number}', user)
        SalesHistory.objects.create(
            product=product, batch=batch, customer=customer, quantity=take,
            unit_price=unit_price, cost_price=batch.cost_price if batch.cost_price is not None else product.cost_price,
            total_price=unit_price * take, receipt_number=receipt_number,
        )
        remaining -= take

    if remaining > 0:  # from unbatched stock
        _movement(product, -remaining, 'out', f'Sale {receipt_number} - unbatched stock', user)
        SalesHistory.objects.create(
            product=product, batch=None, customer=customer, quantity=remaining,
            unit_price=unit_price, cost_price=product.cost_price,
            total_price=unit_price * remaining, receipt_number=receipt_number,
        )

    _set_product_stock(product, int(product.stock_quantity) - quantity, user, f'Sale {receipt_number}')


def restore_from_sale(product, quantity, *, receipt_number, user, reason):
    """Put ``quantity`` units of a sold product back into stock (void/return).

    Units go back to the batches they were sold from (most recent first) and
    the matching SalesHistory rows are reduced so profit reports stay correct.
    ``product`` must already be locked.
    """
    _require_atomic()
    quantity = int(quantity)
    if quantity <= 0:
        raise ValueError(f'Invalid quantity for "{product.name}"')

    remaining = quantity
    history = SalesHistory.objects.select_for_update().filter(
        product=product, receipt_number=receipt_number, quantity__gt=0).order_by('-id')
    for row in history:
        if remaining <= 0:
            break
        give = min(remaining, row.quantity)
        if row.batch_id:
            batch = Batch.objects.select_for_update().get(pk=row.batch_id)
            batch.quantity += give
            batch.save(update_fields=['quantity'])
        row.quantity -= give
        if row.quantity == 0:
            row.delete()
        else:
            row.total_price = row.unit_price * row.quantity
            row.save()  # recomputes profit
        remaining -= give
    # Any remainder (e.g. sales recorded before SalesHistory existed) goes back as unbatched stock.

    _movement(product, quantity, 'in', reason, user)
    _set_product_stock(product, int(product.stock_quantity) + quantity, user, reason)


def receive_batch(batch, quantity, *, user, reason=None):
    """Receive a batch into stock. ``quantity`` is what physically arrived."""
    _require_atomic()
    batch = Batch.objects.select_for_update().get(pk=batch.pk)
    if batch.status == 'received':
        raise ValueError(f'Batch {batch.batch_number} already received')
    quantity = int(quantity)
    if quantity < 0:
        raise ValueError('Received quantity cannot be negative')
    product = lock_products([batch.product_id])[batch.product_id]

    batch.quantity = quantity  # batch now holds what actually arrived
    batch.status = 'received'
    if not batch.received_date:
        batch.received_date = timezone.now().date()
    batch.save()

    if quantity:
        _movement(product, quantity, 'in', reason or f'Batch {batch.batch_number} received', user)
        _set_product_stock(product, int(product.stock_quantity) + quantity, user, f'Batch {batch.batch_number} received')

    if batch.purchase_order_item_id:
        poi = batch.purchase_order_item
        poi.received_quantity += quantity
        poi.save()
        poi.purchase_order.update_status()
    return batch


def remove_batch(batch, *, user, reason):
    """Take a received batch out of sellable stock (recall / expiry write-off)."""
    _require_atomic()
    batch = Batch.objects.select_for_update().get(pk=batch.pk)
    product = lock_products([batch.product_id])[batch.product_id]
    was_received = batch.status == 'received'
    qty = int(batch.quantity) if was_received else 0
    batch.status = 'expired'
    batch.quantity = 0 if was_received else batch.quantity
    batch.save(update_fields=['status', 'quantity'])
    if qty:
        _movement(product, -qty, 'adjustment', reason, user)
        _set_product_stock(product, max(0, int(product.stock_quantity) - qty), user, reason)
    return qty


def adjust(product, delta, *, user, reason):
    """Manual stock correction (count corrections etc.); callers gate it to managers/admins.

    Increases are added as unbatched stock.  Decreases come out of unbatched
    stock first, then out of received batches oldest-expiry first (expired
    batches included, since they are still counted on hand), so batch
    quantities never exceed the product total.  Stock cannot go below zero.
    """
    _require_atomic()
    product = lock_products([product.pk])[product.pk]
    delta = int(delta)
    if delta == 0:
        return product
    new_qty = int(product.stock_quantity) + delta
    if new_qty < 0:
        raise InsufficientStock(
            f'Cannot remove {-delta} units of "{product.name}": only {product.stock_quantity} in stock')
    if delta > 0:
        _movement(product, delta, 'adjustment', reason, user)
    else:
        to_remove = -delta
        from_unbatched = min(to_remove, unbatched_quantity(product))
        if from_unbatched:
            _movement(product, -from_unbatched, 'adjustment', reason, user)
        remaining = to_remove - from_unbatched
        if remaining:
            batches = Batch.objects.select_for_update().filter(
                product=product, status='received', quantity__gt=0
            ).order_by('expiry_date', 'purchase_date', 'id')
            for batch in batches:
                if remaining <= 0:
                    break
                take = min(remaining, batch.quantity)
                batch.quantity -= take
                batch.save(update_fields=['quantity'])
                _movement(product, -take, 'adjustment', f'{reason} - Batch {batch.batch_number}', user)
                remaining -= take
    _set_product_stock(product, new_qty, user, reason)
    return product


def reconcile(product, *, apply=False, user=None):
    """Compare product stock with received batches.

    Only products that track batches can be reconciled; products without
    received batches are reported but never changed (the old recalculation
    zeroed them).  Expired batches still count: they are physically on hand
    until written off with ``remove_batch``.
    """
    received_total = int(_received_batch_total(product))
    has_batches = Batch.objects.filter(product=product, status='received').exists()
    result = {
        'product_id': product.id, 'product_name': product.name,
        'stock_quantity': int(product.stock_quantity),
        'batch_total': received_total, 'tracks_batches': has_batches,
        'difference': received_total - int(product.stock_quantity),
        'applied': False,
    }
    if apply and has_batches and result['difference'] != 0:
        _require_atomic()
        locked = lock_products([product.pk])[product.pk]
        diff = received_total - int(locked.stock_quantity)
        _movement(locked, diff, 'adjustment', 'Reconciled to received batch total', user)
        _set_product_stock(locked, received_total, user, 'Reconciled to received batch total')
        result['applied'] = True
    return result
