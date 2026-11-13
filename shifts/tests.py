from decimal import Decimal

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.test import TestCase
from rest_framework.test import APIClient

from inventory.models import Batch, Product
from sales.models import Return
from shifts.models import Shift


def make_user(username, role='cashier'):
    user = User.objects.create_user(username=username, password='pw-123456')
    from users.models import UserProfile
    UserProfile.objects.update_or_create(user=user, defaults={'role': role})
    user.refresh_from_db()
    return user


class ShiftTests(TestCase):
    def setUp(self):
        self.cashier = make_user('c1')
        self.client = APIClient()
        self.client.force_authenticate(self.cashier)
        self.product = Product.objects.create(sku='B', name='Beer', cost_price=50, selling_price=100,
                                              stock_quantity=0)
        Batch.objects.create(product=self.product, batch_number='B1', quantity=20, status='received',
                             purchase_date='2026-01-01')
        Product.objects.filter(pk=self.product.pk).update(stock_quantity=20)

    def test_endpoints_require_login(self):
        anon = APIClient()
        for url in ('/api/shifts/all/', '/api/shifts/current/'):
            self.assertIn(anon.get(url).status_code, (401, 403), url)
        self.assertIn(anon.post('/api/shifts/end/', {'ending_cash': 0}).status_code, (401, 403))
        self.assertIn(anon.post('/api/shifts/1/force_close/').status_code, (401, 403))

    def test_only_one_open_shift_per_cashier(self):
        self.assertEqual(self.client.post('/api/shifts/start/', {'opening_balance': 100}).status_code, 201)
        self.assertEqual(self.client.post('/api/shifts/start/', {'opening_balance': 100}).status_code, 400)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Shift.objects.create(cashier=self.cashier.userprofile, opening_balance=0, status='open')

    def test_cash_up_uses_cash_payments_and_only_cash_refunds(self):
        self.client.post('/api/shifts/start/', {'opening_balance': 1000})
        # cash sale 300, mpesa sale 200
        s1 = self.client.post('/api/sales/', {'items': [{'product': self.product.id, 'quantity': 3}],
                                              'payment_method': 'cash'}, format='json').data
        self.client.post('/api/sales/', {'items': [{'product': self.product.id, 'quantity': 2}],
                                         'payment_method': 'mpesa'}, format='json')
        item = s1['items'][0]['id']
        # store-credit return (no cash leaves the drawer) and a cash refund of 100
        self.client.post('/api/returns/', {'original_sale_id': s1['id'], 'items': [
            {'sale_item_id': item, 'quantity': 1, 'reason': 'x'}]}, format='json')
        self.client.post('/api/returns/', {'original_sale_id': s1['id'], 'refund_method': 'cash', 'items': [
            {'sale_item_id': item, 'quantity': 1, 'reason': 'x'}]}, format='json')
        self.assertEqual(Return.objects.count(), 2)

        r = self.client.post('/api/shifts/end/', {'ending_cash': 1200}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        rec = r.data['reconciliation']
        self.assertEqual(rec['cash_sales'], 300.0)
        self.assertEqual(rec['mobile_sales'], 200.0)
        self.assertEqual(rec['total_returns'], 200.0)
        self.assertEqual(rec['expected_closing_balance'], 1200.0)  # 1000 + 300 - 100 cash refund
        self.assertEqual(rec['discrepancy'], 0.0)

    def test_cashier_cannot_close_or_see_other_shifts(self):
        other = make_user('c2')
        other_shift = Shift.objects.create(cashier=other.userprofile, opening_balance=0)
        r = self.client.post('/api/shifts/end/', {'shift_id': other_shift.id, 'ending_cash': 0}, format='json')
        self.assertEqual(r.status_code, 403)
        ids = [s['id'] for s in self.client.get('/api/shifts/all/').data['results']]
        self.assertNotIn(other_shift.id, ids)
        self.assertEqual(self.client.post(f'/api/shifts/{other_shift.id}/force_close/').status_code, 403)
