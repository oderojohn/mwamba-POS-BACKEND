from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from .models import Sale, Return
from payments.models import Payment

@receiver(post_save, sender=Sale)
def create_default_payment(sender, instance, created, **kwargs):
    """
    Create a default payment record for sales that don't have any payment.
    This ensures that every sale always has at least one payment record,
    preventing 'N/A' from appearing in payment method displays.
    Only runs for newly created sales.
    """
    # Skip if flagged to skip default payment creation
    if hasattr(instance, '_skip_default_payment') and instance._skip_default_payment:
        return

    if created and not instance.voided and not instance.payment_set.exists():
        # Create a default cash payment for the sale
        Payment.objects.create(
            sale=instance,
            payment_type='cash',
            amount=instance.final_amount,
            status='completed',
            description='Auto-generated default payment'
        )

@receiver(post_save, sender=Return)
def update_shift_return_totals(sender, instance, created, **kwargs):
    """Recompute totals of the shift in which the return was processed."""
    if instance.shift_id:
        instance.shift.recompute_totals()


@receiver(post_delete, sender=Return)
def update_shift_return_totals_on_delete(sender, instance, **kwargs):
    from shifts.models import Shift
    shift = Shift.objects.filter(id=instance.shift_id).first()
    if shift:
        shift.recompute_totals()
