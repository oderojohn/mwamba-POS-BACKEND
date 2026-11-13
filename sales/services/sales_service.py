from django.utils import timezone
from django.db import transaction
from decimal import Decimal
from ..models import Cart
from shifts.models import Shift


def get_held_orders(cashier):
    """Get all held orders for the current cashier's shift"""
    # Return empty list if no cashier provided (no shift started yet)
    if not cashier:
        return Cart.objects.none()
    
    try:
        current_shift = Shift.objects.get(cashier=cashier, status='open')
    except Shift.DoesNotExist:
        # No active shift - return empty list instead of raising error
        return Cart.objects.none()

    held_carts = Cart.objects.filter(
        cashier=cashier,
        status='held'
    ).prefetch_related('cartitem_set__product').order_by('-created_at')

    return held_carts


def void_held_order(cart, void_reason, cashier):
    """Void/cancel a held order with a reason"""
    cart.status = 'voided'
    cart.void_reason = void_reason
    cart.save()

    return cart
