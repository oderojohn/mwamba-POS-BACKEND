"""Sales integrity tests: checkout, idempotency, rollback, discounts, return codes, voids, returns."""
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APIClient

from inventory.models import Batch, Product, SalesHistory, StockMovement
from payments.models import Payment
from shifts.models import Shift

from .models import Cart, Return, ReturnCode, Sale


def make_user(username, role='cashier'):
    user = User.objects.create_user(username=username, password='pw-123456')
    from users.models import UserProfile
    UserProfile.objects.update_or_create(user=user, defaults={'role': role})
    user.refresh_from_db()
    return user


def make_product(name='Beer', price='100.00', stock=0, wholesale=None, sku=None):
    return Product.objects.create(
        sku=sku or name.upper(), name=name, cost_price=Decimal('60.00'),
        selling_price=Decimal(price), wholesale_price=Decimal(wholesale) if wholesale else None,
        stock_quantity=stock)


def add_batch(product, qty, *, status='received', expiry=None, number=None, cost='60.00', days_ago=0):
    batch = Batch.objects.create(
        product=product, batch_number=number or f'B{Batch.objects.count() + 1}', quantity=qty,
        cost_price=Decimal(cost), expiry_date=expiry, status=status,
        purchase_date=date.today() - timedelta(days=days_ago))
    if status == 'received':
        Product.objects.filter(pk=product.pk).update(stock_quantity=product.stock_quantity + qty)
        product.refresh_from_db()
    return batch


class SalesTestBase(TestCase):
    def setUp(self):
        self.cashier = make_user('cashier1')
        self.manager = make_user('manager1', 'manager')
        self.shift = Shift.objects.create(cashier=self.cashier.userprofile, opening_balance=Decimal('1000'))
        self.client = APIClient()
        self.client.force_authenticate(self.cashier)
        self.beer = make_product('Beer', '100.00', wholesale='90.00')
        self.wine = make_product('Wine', '250.00')
        add_batch(self.beer, 10)
        add_batch(self.wine, 5)

    def sell(self, items, method='cash', client=None, **extra):
        payload = {'items': items, 'payment_method': method, **extra}
        return (client or self.client).post('/api/sales/', payload, format='json')

    def stock(self, product):
        product.refresh_from_db()
        return product.stock_quantity


class CheckoutTests(SalesTestBase):
    def test_server_prices_override_client_prices_and_totals(self):
        r = self.sell([{'product': self.beer.id, 'quantity': 2, 'unit_price': 1}], total_amount=2)
        self.assertEqual(r.status_code, 201, r.data)
        sale = Sale.objects.get(id=r.data['id'])
        self.assertEqual(sale.final_amount, Decimal('200.00'))
        self.assertEqual(sale.saleitem_set.get().unit_price, Decimal('100.00'))
        self.assertEqual(Payment.objects.get(sale=sale).amount, Decimal('200.00'))
        self.assertEqual(r.data['amount_due'], '200.00')
        self.assertEqual(self.stock(self.beer), 8)

    def test_wholesale_uses_wholesale_price(self):
        r = self.sell([{'product': self.beer.id, 'quantity': 3}], sale_type='wholesale')
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(Decimal(r.data['final_amount']), Decimal('270.00'))

    def test_same_client_txn_id_creates_one_sale(self):
        body = [{'product': self.beer.id, 'quantity': 1}]
        r1 = self.sell(body, client_txn_id='abc-123')
        r2 = self.sell(body, client_txn_id='abc-123')
        self.assertEqual(r1.status_code, 201)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r1.data['id'], r2.data['id'])
        self.assertEqual(Sale.objects.count(), 1)
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(self.stock(self.beer), 9)

    def test_failure_rolls_back_everything(self):
        """Second line has too little stock: nothing from the first line may be saved."""
        r = self.sell([{'product': self.beer.id, 'quantity': 2}, {'product': self.wine.id, 'quantity': 50}])
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data['error_code'], 'insufficient_stock')
        self.assertEqual(Sale.objects.count(), 0)
        self.assertEqual(Cart.objects.count(), 0)
        self.assertEqual(Payment.objects.count(), 0)
        self.assertEqual(self.stock(self.beer), 10)
        self.assertEqual(StockMovement.objects.count(), 0)
        self.shift.refresh_from_db()
        self.assertEqual(self.shift.total_sales, 0)

    def test_split_payment_mismatch_rolls_back(self):
        r = self.sell([{'product': self.beer.id, 'quantity': 1}], method='split',
                      split_data={'cash': 50, 'mpesa': 20})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data['error_code'], 'amount_mismatch')
        self.assertEqual(r.data['expected_amount_due'], '100.00')
        self.assertEqual(Sale.objects.count(), 0)
        self.assertEqual(self.stock(self.beer), 10)

    def test_split_payment_records_both_parts(self):
        r = self.sell([{'product': self.beer.id, 'quantity': 1}], method='split',
                      split_data={'cash': 60, 'mpesa': 40})
        self.assertEqual(r.status_code, 201, r.data)
        self.shift.refresh_from_db()
        self.assertEqual(self.shift.cash_sales, Decimal('60'))
        self.assertEqual(self.shift.mobile_sales, Decimal('40'))
        self.assertEqual(self.shift.total_sales, Decimal('100'))

    def test_cashier_cannot_discount(self):
        r = self.sell([{'product': self.beer.id, 'quantity': 1, 'discount': 10}])
        self.assertEqual(r.status_code, 403)
        r = self.sell([{'product': self.beer.id, 'quantity': 1}], discount_amount=5)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(Sale.objects.count(), 0)

    def test_manager_can_discount(self):
        Shift.objects.create(cashier=self.manager.userprofile, opening_balance=0)
        mc = APIClient()
        mc.force_authenticate(self.manager)
        r = self.sell([{'product': self.beer.id, 'quantity': 2, 'discount': 10}], client=mc, discount_amount=5)
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(Decimal(r.data['final_amount']), Decimal('185.00'))

    def test_requires_open_shift(self):
        self.shift.status = 'closed'
        self.shift.save()
        r = self.sell([{'product': self.beer.id, 'quantity': 1}])
        self.assertEqual(r.status_code, 400)
        self.assertEqual(Sale.objects.count(), 0)

    def test_sales_cannot_be_patched_or_deleted(self):
        sale_id = self.sell([{'product': self.beer.id, 'quantity': 1}]).data['id']
        self.assertEqual(self.client.patch(f'/api/sales/{sale_id}/', {'voided': True}, format='json').status_code, 405)
        self.assertEqual(self.client.delete(f'/api/sales/{sale_id}/').status_code, 405)
        self.assertFalse(Sale.objects.get(id=sale_id).voided)

    def test_held_order_completion_is_priced_and_idempotent(self):
        r = self.client.post('/api/sales/', {'hold_order': True, 'items': [
            {'product': self.beer.id, 'quantity': 2, 'unit_price': 1}]}, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(self.stock(self.beer), 10)  # holding takes no stock
        cart_id = r.data['cart_id']
        url = f'/api/sales/{cart_id}/complete_held_order/'
        r1 = self.client.post(url, {'payment_method': 'cash'}, format='json')
        r2 = self.client.post(url, {'payment_method': 'cash'}, format='json')
        self.assertEqual(r1.status_code, 201, r1.data)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(Decimal(r1.data['final_amount']), Decimal('200.00'))
        self.assertEqual(Sale.objects.count(), 1)
        self.assertEqual(self.stock(self.beer), 8)


class ReturnCodeTests(SalesTestBase):
    def make_code(self, amount):
        return ReturnCode.objects.create(code='ABCD1234', refund_amount=Decimal(amount),
                                         original_receipt_number='R-1')

    def test_validate_endpoint(self):
        self.make_code('150')
        r = self.client.post('/api/returns/return-codes/validate/', {'code': 'abcd1234'}, format='json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['refund_amount'], '150.00')
        r = self.client.post('/api/returns/return-codes/validate/', {'code': 'NOPE0000'}, format='json')
        self.assertEqual(r.status_code, 404)

    def test_credit_reduces_amount_due_and_code_cannot_be_reused(self):
        self.make_code('150')
        r = self.sell([{'product': self.wine.id, 'quantity': 1}], return_code={'code': 'ABCD1234', 'amount': 9999})
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data['amount_due'], '100.00')
        self.assertEqual(Decimal(r.data['return_code_amount']), Decimal('150.00'))
        self.assertEqual(Payment.objects.get().amount, Decimal('100.00'))
        self.assertEqual(ReturnCode.objects.get(code='ABCD1234').status, 'used')
        r = self.sell([{'product': self.beer.id, 'quantity': 1}], return_code={'code': 'ABCD1234'})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(Sale.objects.count(), 1)

    def test_leftover_credit_becomes_new_code(self):
        self.make_code('300')
        r = self.sell([{'product': self.beer.id, 'quantity': 1}], return_code={'code': 'ABCD1234'})
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data['amount_due'], '0.00')
        self.assertEqual(Payment.objects.count(), 0)
        self.assertEqual(r.data['remaining_return_code']['refund_amount'], '200.00')

    def test_invalid_code_rolls_back_sale(self):
        r = self.sell([{'product': self.beer.id, 'quantity': 1}], return_code={'code': 'FAKE9999'})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(Sale.objects.count(), 0)
        self.assertEqual(self.stock(self.beer), 10)


class VoidTests(SalesTestBase):
    def test_void_restores_stock_batches_payments_and_shift(self):
        sale_id = self.sell([{'product': self.beer.id, 'quantity': 3}]).data['id']
        batch = Batch.objects.get(product=self.beer)
        self.assertEqual(batch.quantity, 7)
        r = self.client.post(f'/api/sales/{sale_id}/void_sale/', {'reason': 'Wrong item'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        batch.refresh_from_db()
        self.assertEqual(batch.quantity, 10)
        self.assertEqual(self.stock(self.beer), 10)
        self.assertFalse(SalesHistory.objects.filter(receipt_number=Sale.objects.get(id=sale_id).receipt_number).exists())
        self.assertEqual(Payment.objects.get().status, 'refunded')
        self.shift.refresh_from_db()
        self.assertEqual(self.shift.total_sales, 0)
        self.assertEqual(self.shift.cash_sales, 0)
        # second void is rejected and does not restock again
        r = self.client.post(f'/api/sales/{sale_id}/void_sale/', {'reason': 'again'}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.stock(self.beer), 10)

    def test_cannot_void_sale_with_returns(self):
        r = self.sell([{'product': self.beer.id, 'quantity': 3}])
        item_id = r.data['items'][0]['id']
        self.client.post('/api/returns/', {'original_sale_id': r.data['id'], 'items': [
            {'sale_item_id': item_id, 'quantity': 1, 'reason': 'Broken'}]}, format='json')
        r2 = self.client.post(f"/api/sales/{r.data['id']}/void_sale/", {'reason': 'x'}, format='json')
        self.assertEqual(r2.status_code, 400)
        self.assertEqual(self.stock(self.beer), 8)  # 10 - 3 + 1 returned, no double restore

    def test_cashier_cannot_void_someone_elses_sale(self):
        sale_id = self.sell([{'product': self.beer.id, 'quantity': 1}]).data['id']
        other = make_user('cashier2')
        oc = APIClient()
        oc.force_authenticate(other)
        r = oc.post(f'/api/sales/{sale_id}/void_sale/', {'reason': 'x'}, format='json')
        self.assertEqual(r.status_code, 403)
        r = oc.post(f'/api/sales/{sale_id}/admin_void_sale/', {'reason': 'x'}, format='json')
        self.assertEqual(r.status_code, 403)

    def test_void_gives_return_code_credit_back(self):
        ReturnCode.objects.create(code='ABCD1234', refund_amount=Decimal('300'), original_receipt_number='R-1')
        sale_id = self.sell([{'product': self.beer.id, 'quantity': 1}], return_code={'code': 'ABCD1234'}).data['id']
        mc = APIClient()
        mc.force_authenticate(self.manager)
        r = mc.post(f'/api/sales/{sale_id}/admin_void_sale/', {'reason': 'x'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(ReturnCode.objects.get(code='ABCD1234').status, 'active')
        self.assertEqual(ReturnCode.objects.get(issued_from_sale_id=sale_id).status, 'cancelled')

    def test_edit_endpoints_are_disabled(self):
        sale_id = self.sell([{'product': self.beer.id, 'quantity': 1}]).data['id']
        mc = APIClient()
        mc.force_authenticate(self.manager)
        self.assertEqual(mc.post(f'/api/sales/{sale_id}/void_items/', {}, format='json').status_code, 410)
        self.assertEqual(mc.patch(f'/api/sales/{sale_id}/admin_edit_sale/', {}, format='json').status_code, 410)


class ReturnTests(SalesTestBase):
    def setUp(self):
        super().setUp()
        r = self.sell([{'product': self.beer.id, 'quantity': 4}, {'product': self.wine.id, 'quantity': 1}])
        self.sale = Sale.objects.get(id=r.data['id'])
        self.items = {i['product']: i['id'] for i in r.data['items']}

    def ret(self, items, **extra):
        return self.client.post('/api/returns/', {'original_sale_id': self.sale.id, 'items': items, **extra},
                                format='json')

    def test_return_restocks_batch_issues_code_and_reduces_sales_history(self):
        r = self.ret([{'sale_item_id': self.items[self.beer.id], 'quantity': 2, 'reason': 'Flat'}])
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(Decimal(r.data['total_refund_amount']), Decimal('200.00'))
        self.assertTrue(ReturnCode.objects.filter(code=r.data['return_code'], refund_amount=Decimal('200')).exists())
        self.assertEqual(self.stock(self.beer), 8)
        self.assertEqual(Batch.objects.get(product=self.beer).quantity, 8)
        sold = sum(h.quantity for h in SalesHistory.objects.filter(product=self.beer))
        self.assertEqual(sold, 2)

    def test_duplicate_lines_cannot_exceed_sold_quantity(self):
        line = {'sale_item_id': self.items[self.beer.id], 'quantity': 3, 'reason': 'x'}
        r = self.ret([line, line])
        self.assertEqual(r.status_code, 400)
        self.assertEqual(Return.objects.count(), 0)
        self.assertEqual(self.stock(self.beer), 6)

    def test_cannot_return_more_than_remaining_over_two_returns(self):
        line = {'sale_item_id': self.items[self.beer.id], 'quantity': 3, 'reason': 'x'}
        self.assertEqual(self.ret([line]).status_code, 201)
        self.assertEqual(self.ret([line]).status_code, 400)

    def test_item_from_another_sale_rejected(self):
        other = self.sell([{'product': self.beer.id, 'quantity': 1}]).data
        r = self.ret([{'sale_item_id': other['items'][0]['id'], 'quantity': 1, 'reason': 'x'}])
        self.assertEqual(r.status_code, 404)

    def test_voided_sale_cannot_be_returned(self):
        mc = APIClient()
        mc.force_authenticate(self.manager)
        mc.post(f'/api/sales/{self.sale.id}/admin_void_sale/', {'reason': 'x'}, format='json')
        r = self.ret([{'sale_item_id': self.items[self.beer.id], 'quantity': 1, 'reason': 'x'}])
        self.assertEqual(r.status_code, 400)

    def test_refund_respects_discount(self):
        Shift.objects.create(cashier=self.manager.userprofile, opening_balance=0)
        mc = APIClient()
        mc.force_authenticate(self.manager)
        r = self.sell([{'product': self.beer.id, 'quantity': 2, 'discount': 20}], client=mc)
        self.assertEqual(r.status_code, 201, r.data)
        rr = mc.post('/api/returns/', {'original_sale_id': r.data['id'], 'items': [
            {'sale_item_id': r.data['items'][0]['id'], 'quantity': 1, 'reason': 'x'}]}, format='json')
        self.assertEqual(rr.status_code, 201, rr.data)
        self.assertEqual(Decimal(rr.data['total_refund_amount']), Decimal('90.00'))
