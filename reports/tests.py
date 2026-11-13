from datetime import date
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APIClient

from inventory.models import Batch, Product
from sales.models import Sale
from shifts.models import Shift
from users.models import UserProfile

from .views import sales_profit


class SalesProfitTests(TestCase):
    """Revenue/profit exclude voided sales and returned units."""

    def setUp(self):
        self.user = User.objects.create_user('mgr', password='pw-123456')
        UserProfile.objects.update_or_create(user=self.user, defaults={'role': 'manager'})
        self.user.refresh_from_db()
        Shift.objects.create(cashier=self.user.userprofile, opening_balance=0)
        self.p = Product.objects.create(sku='B', name='Beer', cost_price=Decimal('60'),
                                        selling_price=Decimal('100'), stock_quantity=20)
        Batch.objects.create(product=self.p, batch_number='B1', quantity=20, status='received',
                             purchase_date=date.today(), cost_price=Decimal('60'))
        self.c = APIClient()
        self.c.force_authenticate(self.user)

    def sell(self, qty):
        r = self.c.post('/api/sales/', {'items': [{'product': self.p.id, 'quantity': qty}],
                                         'payment_method': 'cash'}, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        return r.data

    def test_voids_and_returns_are_excluded(self):
        kept = self.sell(3)                       # 300 revenue, 180 cost
        voided = self.sell(5)
        self.c.post(f"/api/sales/{voided['id']}/admin_void_sale/", {'reason': 'x'}, format='json')
        self.c.post('/api/returns/', {'original_sale_id': kept['id'], 'items': [
            {'sale_item_id': kept['items'][0]['id'], 'quantity': 1, 'reason': 'x'}]}, format='json')

        t = sales_profit(Sale.objects.all())
        self.assertEqual(t['transactions'], 1)
        self.assertEqual(t['revenue'], 200.0)           # 300 - 100 refund
        self.assertEqual(t['cost_of_goods_sold'], 120.0)  # 2 units kept x 60
        self.assertEqual(t['gross_profit'], 80.0)

    def test_summary_endpoints_respond(self):
        self.sell(2)
        for params in ('', '?all_shifts=1', f'?date_from={date.today()}&date_to={date.today()}'):
            r = self.c.get(f'/api/reports/sales-summary/{params}')
            self.assertEqual(r.status_code, 200, (params, getattr(r, 'data', None)))

    def test_all_shifts_defaults_to_30_days_and_counts_every_shift(self):
        self.sell(1)
        r = self.c.get('/api/reports/sales-summary/?all_shifts=1')
        self.assertEqual(r.status_code, 200)
        self.assertIsNotNone(r.data['date_from'])            # no longer "every shift ever"
        self.assertEqual(r.data['shift_count'], 1)
        self.assertEqual(r.data['total_sales'], 100.0)
        self.assertIsNone(r.data['details_limited_to'])
        self.assertEqual(len(r.data['shifts'][0]['sales']), 1)  # itemised detail present

    def test_date_range_report_profit_matches_items(self):
        self.sell(3)
        today = date.today()
        rows = self.c.get(f'/api/reports/sales-summary/?date_from={today}&date_to={today}').data
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['total_sales'], 300.0)
        self.assertEqual(rows[0]['cost_of_goods_sold'], 180.0)
        self.assertEqual(rows[0]['gross_profit'], 120.0)
