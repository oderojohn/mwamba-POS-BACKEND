"""Tests for the single stock service (inventory/stock.py)."""
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import transaction
from django.test import TestCase, TransactionTestCase
from rest_framework.test import APIClient

from inventory import stock
from inventory.models import Batch, Product, ProductHistory, StockMovement


class StockServiceTests(TestCase):
    def setUp(self):
        self.p = Product.objects.create(sku='W', name='Whisky', cost_price=Decimal('500'),
                                        selling_price=Decimal('800'), stock_quantity=0)

    def batch(self, qty, *, status='received', expiry=None, days_ago=0, cost='500'):
        b = Batch.objects.create(product=self.p, batch_number=f'B{Batch.objects.count() + 1}', quantity=qty,
                                 status=status, expiry_date=expiry, cost_price=Decimal(cost),
                                 purchase_date=date.today() - timedelta(days=days_ago))
        if status == 'received':
            Product.objects.filter(pk=self.p.pk).update(stock_quantity=self.p.stock_quantity + qty)
            self.p.refresh_from_db()
        return b

    def sell(self, qty, receipt='S1'):
        with transaction.atomic():
            p = stock.lock_products([self.p.id])[self.p.id]
            stock.deduct_for_sale(p, qty, unit_price=Decimal('800'), receipt_number=receipt, user=None)
        self.p.refresh_from_db()

    def test_fifo_by_expiry_across_batches(self):
        later = self.batch(5, expiry=date.today() + timedelta(days=60))
        sooner = self.batch(5, expiry=date.today() + timedelta(days=10))
        self.sell(7)
        sooner.refresh_from_db(); later.refresh_from_db()
        self.assertEqual((sooner.quantity, later.quantity), (0, 3))
        self.assertEqual(self.p.stock_quantity, 3)
        self.assertEqual(StockMovement.objects.filter(movement_type='out').count(), 2)
        self.assertTrue(ProductHistory.objects.filter(product=self.p, field_changed='stock_quantity').exists())

    def test_ordered_and_expired_batches_are_not_sold(self):
        self.batch(5, status='ordered')
        self.batch(5, expiry=date.today() - timedelta(days=1))
        self.batch(2)
        with self.assertRaises(stock.InsufficientStock):
            self.sell(3)
        self.p.refresh_from_db()
        self.assertEqual(self.p.stock_quantity, 7)  # unchanged

    def test_unbatched_legacy_stock_can_be_sold(self):
        Product.objects.filter(pk=self.p.pk).update(stock_quantity=4)
        self.p.refresh_from_db()
        self.sell(3)
        self.assertEqual(self.p.stock_quantity, 1)

    def test_restore_returns_units_to_original_batch(self):
        b = self.batch(5)
        self.sell(4)
        with transaction.atomic():
            p = stock.lock_products([self.p.id])[self.p.id]
            stock.restore_from_sale(p, 4, receipt_number='S1', user=None, reason='void')
        b.refresh_from_db(); self.p.refresh_from_db()
        self.assertEqual((b.quantity, self.p.stock_quantity), (5, 5))

    def test_receive_partial_batch_sets_actual_quantity(self):
        b = self.batch(10, status='ordered')
        with transaction.atomic():
            stock.receive_batch(b, 7, user=None)
        b.refresh_from_db(); self.p.refresh_from_db()
        self.assertEqual((b.status, b.quantity, self.p.stock_quantity), ('received', 7, 7))

    def test_reconcile_never_zeroes_products_without_batches(self):
        Product.objects.filter(pk=self.p.pk).update(stock_quantity=12)
        self.p.refresh_from_db()
        with transaction.atomic():
            r = stock.reconcile(self.p, apply=True)
        self.p.refresh_from_db()
        self.assertFalse(r['applied'])
        self.assertEqual(self.p.stock_quantity, 12)
        # reading the old property no longer writes
        self.assertEqual(self.p.recalculate_stock_from_batches(), 0)
        self.p.refresh_from_db()
        self.assertEqual(self.p.stock_quantity, 12)


class StockEndpointTests(TestCase):
    def setUp(self):
        from users.models import UserProfile
        self.user = User.objects.create_user('m', password='pw-123456')
        UserProfile.objects.update_or_create(user=self.user, defaults={'role': 'manager'})
        self.cashier = User.objects.create_user('c', password='pw-123456')
        UserProfile.objects.update_or_create(user=self.cashier, defaults={'role': 'cashier'})
        self.user.refresh_from_db(); self.cashier.refresh_from_db()
        self.p = Product.objects.create(sku='G', name='Gin', cost_price=10, selling_price=20, stock_quantity=5)

    def test_recalculate_is_report_only_by_default(self):
        c = APIClient(); c.force_authenticate(self.user)
        r = c.post('/api/inventory/products/recalculate_stock/', {}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.p.refresh_from_db()
        self.assertEqual(self.p.stock_quantity, 5)

    def test_stock_movements_are_read_only(self):
        c = APIClient(); c.force_authenticate(self.user)
        r = c.post('/api/inventory/stock-movements/', {'product': self.p.id, 'movement_type': 'in',
                                                       'quantity': 5}, format='json')
        self.assertEqual(r.status_code, 405)

    def test_product_stock_edit_goes_through_adjust_and_needs_manager(self):
        cc = APIClient(); cc.force_authenticate(self.cashier)
        r = cc.patch(f'/api/inventory/products/{self.p.id}/', {'stock_quantity': 50}, format='json')
        self.assertEqual(r.status_code, 403)
        mc = APIClient(); mc.force_authenticate(self.user)
        r = mc.patch(f'/api/inventory/products/{self.p.id}/', {'stock_quantity': 8}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.p.refresh_from_db()
        self.assertEqual(self.p.stock_quantity, 8)
        self.assertTrue(StockMovement.objects.filter(product=self.p, quantity=3, movement_type='adjustment').exists())


class StockTransactionGuardTests(TransactionTestCase):
    def test_requires_transaction(self):
        p = Product.objects.create(sku='X', name='X', cost_price=1, selling_price=2, stock_quantity=1)
        with self.assertRaises(RuntimeError):
            stock.lock_products([p.id])
