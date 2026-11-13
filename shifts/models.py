from django.db import models

# Create your models here.

class Shift(models.Model):
    STATUS_CHOICES = [
        ('open', 'Open'),
        ('closed', 'Closed'),
    ]
    
    cashier = models.ForeignKey('users.UserProfile', on_delete=models.CASCADE)
    start_time = models.DateTimeField(auto_now_add=True)
    end_time = models.DateTimeField(null=True, blank=True)
    opening_balance = models.DecimalField(max_digits=10, decimal_places=2)
    closing_balance = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    cash_sales = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    card_sales = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    mobile_sales = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_sales = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    # Return fields
    total_returns = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    return_count = models.IntegerField(default=0)
    net_sales = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='open')
    discrepancy = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    approved_by = models.ForeignKey('users.UserProfile', on_delete=models.SET_NULL, null=True, blank=True, related_name='approved_shifts')
    notes = models.TextField(blank=True, null=True)
    
    def __str__(self):
        return f"Shift {self.id} - {self.cashier.user.username} - {self.status}"
    
    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['cashier'], condition=models.Q(status='open'),
                name='one_open_shift_per_cashier',
            ),
        ]

    def recompute_totals(self, save=True):
        """Derive all shift totals from Payment and Return records.

        cash/mobile/card sales = completed payments on non-voided sales in this shift.
        total_sales = final amount of non-voided sales (includes the part settled
        with return-code credit, which is not a payment).
        total_returns = refunds of returns processed in this shift.
        """
        from django.db.models import Sum, Count, Q
        from decimal import Decimal
        from payments.models import Payment
        from sales.models import Return

        pay = Payment.objects.filter(sale__shift=self, sale__voided=False, status='completed')
        by_type = pay.aggregate(
            cash=Sum('amount', filter=Q(payment_type='cash')),
            mobile=Sum('amount', filter=Q(payment_type='mpesa')),
        )
        sales = self.sale_set.filter(voided=False).aggregate(total=Sum('final_amount'))
        rets = Return.objects.filter(shift=self).aggregate(
            total=Sum('total_refund_amount'), count=Count('id'),
            cash=Sum('total_refund_amount', filter=Q(refund_method='cash')),
        )
        zero = Decimal('0')
        self.cash_sales = by_type['cash'] or zero
        self.mobile_sales = by_type['mobile'] or zero
        self.card_sales = zero
        self.total_sales = sales['total'] or zero
        self.total_returns = rets['total'] or zero
        self.return_count = rets['count'] or 0
        self.net_sales = self.total_sales - self.total_returns
        self._cash_refunds = rets['cash'] or zero
        if save:
            self.save(update_fields=['cash_sales', 'mobile_sales', 'card_sales', 'total_sales',
                                     'total_returns', 'return_count', 'net_sales'])
        return self

    def expected_cash(self):
        """Cash that should be in the drawer: opening + cash taken - cash refunded."""
        self.recompute_totals(save=False)
        return self.opening_balance + self.cash_sales - self._cash_refunds

    # Backwards-compatible names used by signals/management commands
    def update_return_totals(self):
        return self.recompute_totals()

    def refresh_totals(self):
        return self.recompute_totals()
