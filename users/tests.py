from django.contrib.auth.models import Group, User
from django.test import TestCase
from rest_framework.test import APIClient

from inventory.models import Product
from users.models import UserProfile
from users.permissions import has_manager_rights


def make_user(username, role='cashier', groups=()):
    user = User.objects.create_user(username, f'{username}@example.com', 'pw')
    profile, _ = UserProfile.objects.get_or_create(user=user)
    profile.role = role
    profile.save()
    for name in groups:
        user.groups.add(Group.objects.get_or_create(name=name)[0])
    return user


class ManagerRightsTests(TestCase):
    def test_roles_and_groups(self):
        self.assertTrue(has_manager_rights(make_user('a', role='admin')))
        self.assertTrue(has_manager_rights(make_user('m', role='manager')))
        # Login shows admin screens to the "admin" group, so it must have the rights too
        self.assertTrue(has_manager_rights(make_user('g', groups=['admin'])))
        self.assertTrue(has_manager_rights(make_user('G', groups=['Admin'])))
        self.assertFalse(has_manager_rights(make_user('c', groups=['pos'])))

    def _edit_stock(self, user):
        product = Product.objects.create(sku='S1', name='Beer', cost_price=10,
                                         selling_price=20, stock_quantity=0)
        client = APIClient()
        client.force_authenticate(user)
        return client.patch(f'/api/inventory/products/{product.id}/',
                            {'stock_quantity': 12}, format='json'), product

    def test_admin_group_can_change_stock(self):
        response, product = self._edit_stock(make_user('admin', groups=['admin']))
        self.assertEqual(response.status_code, 200, response.content)
        product.refresh_from_db()
        self.assertEqual(product.stock_quantity, 12)

    def test_cashier_cannot_change_stock(self):
        response, product = self._edit_stock(make_user('cashier', groups=['pos']))
        self.assertEqual(response.status_code, 403)
        product.refresh_from_db()
        self.assertEqual(product.stock_quantity, 0)
