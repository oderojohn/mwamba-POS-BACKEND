from rest_framework import viewsets, status, generics
from rest_framework.response import Response
from rest_framework.decorators import action
from django.db.models import Sum, Count, Avg, F, Q
from django.db.models.functions import TruncDate, ExtractHour
from django.utils import timezone
from datetime import timedelta, datetime
from decimal import Decimal
from .models import (
    Report, SalesReport, ProductSalesHistory, CustomerAnalytics,
    InventoryAnalytics, ShiftAnalytics, ProfitLossReport
)
from .serializers import (
    ReportSerializer, SalesReportSerializer, ProductSalesHistorySerializer,
    CustomerAnalyticsSerializer, InventoryAnalyticsSerializer,
    ShiftAnalyticsSerializer, ProfitLossReportSerializer,
    SalesSummarySerializer, InventorySummarySerializer,
    CustomerSummarySerializer, ShiftSummarySerializer
)

class ReportViewSet(viewsets.ModelViewSet):
    queryset = Report.objects.all()
    serializer_class = ReportSerializer

    @action(detail=False, methods=['post'])
    def generate_sales_report(self, request):
        """Generate sales report for date range"""
        date_from = request.data.get('date_from')
        date_to = request.data.get('date_to')

        if not date_from or not date_to:
            return Response({'error': 'Date range is required'}, status=status.HTTP_400_BAD_REQUEST)

        # Get sales data
        sales_data = self._get_sales_data(date_from, date_to)

        # Create report record
        report = Report.objects.create(
            report_type='sales',
            title=f'Sales Report ({date_from} to {date_to})',
            date_from=date_from,
            date_to=date_to,
            generated_by=request.user.userprofile if hasattr(request.user, 'userprofile') else None,
            data=sales_data,
            total_records=len(sales_data)
        )

        # Return the actual report data, not the Report object
        return Response(sales_data, status=status.HTTP_200_OK)

    @action(detail=False, methods=['post'])
    def generate_inventory_report(self, request):
        """Generate inventory report"""
        inventory_data = self._get_inventory_data()

        # Return the actual inventory data
        return Response(inventory_data, status=status.HTTP_200_OK)

    @action(detail=False, methods=['post'])
    def generate_customer_report(self, request):
        """Generate customer analytics report"""
        customer_data = self._get_customer_data()

        # Return the actual customer data
        return Response(customer_data, status=status.HTTP_200_OK)

    @action(detail=False, methods=['post'])
    def generate_profit_loss_report(self, request):
        """Generate profit & loss report"""
        date_from = request.data.get('date_from')
        date_to = request.data.get('date_to')

        if not date_from or not date_to:
            return Response({'error': 'Date range is required'}, status=status.HTTP_400_BAD_REQUEST)

        pl_data = self._calculate_profit_loss(date_from, date_to)

        # Return the actual profit & loss data
        return Response(pl_data, status=status.HTTP_200_OK)

    def _get_sales_data(self, date_from, date_to):
        """Get sales data for the specified date range"""
        from sales.models import Sale, Payment
        from django.db.models import Case, When, DecimalField

        sales = Sale.objects.filter(
            sale_date__date__range=[date_from, date_to]
        ).annotate(
            date=TruncDate('sale_date')
        ).values('date').annotate(
            total_sales=Sum('final_amount'),
            transaction_count=Count('id')
        ).order_by('date')

        # Get payment method breakdown
        payments = Payment.objects.filter(
            sale__sale_date__date__range=[date_from, date_to],
            status='completed'
        ).values('payment_type').annotate(
            amount=Sum('amount')
        )

        payment_breakdown = {}
        for payment in payments:
            payment_breakdown[payment['payment_type']] = float(payment['amount'])

        result = []
        for sale in sales:
            result.append({
                'date': sale['date'].strftime('%Y-%m-%d'),
                'total_sales': float(sale['total_sales']),
                'cash_sales': float(payment_breakdown.get('cash', 0)),
                'card_sales': float(payment_breakdown.get('card', 0)),
                'mobile_sales': float(payment_breakdown.get('mpesa', 0)),  # Frontend expects 'mobile_sales'
                'transactions': sale['transaction_count'],  # Frontend expects 'transactions'
                'gross_profit': float(sale['total_sales'] * Decimal('0.25')),  # Estimated 25% margin
                'net_profit': float(sale['total_sales'] * Decimal('0.20'))  # Estimated 20% net profit
            })

        return result

    def _get_inventory_data(self):
        """Get current inventory data"""
        from inventory.models import Product

        products = Product.objects.all().select_related('category')

        result = []
        for product in products:
            # Calculate stock status
            if product.stock_quantity == 0:
                status = 'out_of_stock'
            elif product.stock_quantity <= (product.low_stock_threshold or 10):
                status = 'low_stock'
            elif product.stock_quantity > (product.low_stock_threshold or 10) * 3:
                status = 'overstock'
            else:
                status = 'in_stock'

            result.append({
                'product': product.name,
                'category': product.category.name if product.category else 'Uncategorized',
                'stock_level': product.stock_quantity,
                'sold_today': 0,  # Would be calculated from actual sales data
                'value': float(product.stock_quantity * product.cost_price),
            })

        return result

    def _get_customer_data(self):
        """Get customer analytics data"""
        from customers.models import Customer
        from sales.models import Sale

        customers = Customer.objects.all()

        result = []
        for customer in customers:
            customer_sales = Sale.objects.filter(customer=customer)
            total_purchases = customer_sales.aggregate(total=Sum('final_amount'))['total'] or 0
            order_count = customer_sales.count()

            last_purchase = customer_sales.order_by('-sale_date').first()
            last_purchase_date = last_purchase.sale_date if last_purchase else None

            result.append({
                'name': customer.name,
                'phone': customer.phone,
                'total_purchases': float(total_purchases),
                'last_visit': last_purchase_date.strftime('%Y-%m-%d') if last_purchase_date else None,
                'loyalty_points': customer.loyalty_points,
            })

        return sorted(result, key=lambda x: x['total_purchases'], reverse=True)

    def _calculate_profit_loss(self, date_from, date_to):
        """Calculate profit & loss for the period"""
        from sales.models import Sale
        from inventory.models import SalesHistory

        # Calculate revenue
        sales = Sale.objects.filter(sale_date__date__range=[date_from, date_to])
        total_revenue = sales.aggregate(total=Sum('final_amount'))['total'] or 0

        # Calculate cost of goods sold
        sales_history = SalesHistory.objects.filter(
            sale_date__date__range=[date_from, date_to]
        )
        cost_of_goods_sold = sales_history.aggregate(
            total_cost=Sum(F('cost_price') * F('quantity'))
        )['total_cost'] or 0

        gross_profit = float(total_revenue) - float(cost_of_goods_sold)

        # Estimated operating expenses (25% of revenue)
        operating_expenses = float(total_revenue) * 0.25

        net_profit = gross_profit - operating_expenses
        profit_margin = (net_profit / float(total_revenue)) * 100 if total_revenue > 0 else 0

        return {
            'date_from': date_from,
            'date_to': date_to,
            'total_revenue': float(total_revenue),
            'cost_of_goods_sold': float(cost_of_goods_sold),
            'gross_profit': gross_profit,
            'operating_expenses': operating_expenses,
            'net_profit': net_profit,
            'profit_margin_percentage': profit_margin
        }

class SalesSummaryView(generics.GenericAPIView):
    """Get sales summary for dashboard and reports"""

    def get(self, request):
        # Check if shift_id is provided (for shift-specific sales summary)
        shift_id = request.query_params.get('shift_id')

        if shift_id:
            # Return sales data for the specified shift
            sales_data = self._get_shift_sales_data(shift_id)
            return Response(sales_data)

        # Check if date range is provided (for reports)
        date_from = request.query_params.get('date_from')
        date_to = request.query_params.get('date_to')

        if date_from and date_to:
            # Return historical sales data for the specified range
            sales_data = self._get_sales_data_for_range(date_from, date_to)
            return Response(sales_data)
        else:
            # Return dashboard summary data
            today = timezone.now().date()
            week_ago = today - timedelta(days=7)

            # Today's sales
            today_sales = self._get_today_sales()

            # Total sales
            total_sales = self._get_total_sales()

            # Sales data for chart (last 7 days)
            sales_data = self._get_sales_trend_data(week_ago, today)

            # Payment methods distribution
            payment_methods = self._get_payment_methods_data(today)

            # Top products
            top_products = self._get_top_products(week_ago, today)

            # Recent transactions
            recent_transactions = self._get_recent_transactions()

            data = {
                'today_sales': today_sales,
                'total_sales': total_sales,
                'sales_data': sales_data,
                'payment_methods': payment_methods,
                'top_products': top_products,
                'recent_transactions': recent_transactions
            }

            serializer = SalesSummarySerializer(data)
            return Response(serializer.data)

    def _get_today_sales(self):
        from sales.models import Sale
        today_sales = Sale.objects.filter(
            sale_date__date=timezone.now().date()
        ).aggregate(total=Sum('final_amount'))['total'] or Decimal('0')
        return float(today_sales)

    def _get_total_sales(self):
        from sales.models import Sale
        total_sales = Sale.objects.aggregate(total=Sum('final_amount'))['total'] or Decimal('0')
        return float(total_sales)

    def _get_sales_trend_data(self, start_date, end_date):
        from sales.models import Sale
        from django.db.models.functions import TruncDate

        sales_trend = Sale.objects.filter(
            sale_date__date__range=[start_date, end_date]
        ).annotate(
            date=TruncDate('sale_date')
        ).values('date').annotate(
            amount=Sum('final_amount')
        ).order_by('date')

        result = [
            {
                'date': item['date'].strftime('%Y-%m-%d'),
                'amount': float(item['amount'] or 0)
            }
            for item in sales_trend
        ]

        # If no data, return empty array (frontend will handle fallback)
        return result

    def _get_payment_methods_data(self, date):
        from payments.models import Payment

        payments = Payment.objects.filter(
            created_at__date=date,
            status='completed'
        ).values('payment_type').annotate(
            amount=Sum('amount')
        ).order_by('-amount')

        total_amount = sum(float(p['amount']) for p in payments)

        return [
            {
                'method': payment['payment_type'],
                'amount': float(payment['amount']),
                'percentage': (float(payment['amount']) / total_amount * 100) if total_amount > 0 else 0
            }
            for payment in payments
        ]

    def _get_top_products(self, start_date, end_date):
        from sales.models import SaleItem
        from django.db.models import Sum

        top_products = SaleItem.objects.filter(
            sale__sale_date__date__range=[start_date, end_date]
        ).values(
            'product__name'
        ).annotate(
            sold=Sum('quantity'),
            revenue=Sum(F('unit_price') * F('quantity'))
        ).order_by('-sold')[:5]

        return [
            {
                'name': item['product__name'],
                'sold': item['sold'],
                'revenue': float(item['revenue'])
            }
            for item in top_products
        ]

    def _get_recent_transactions(self):
        from sales.models import Sale

        recent_sales = Sale.objects.select_related('customer').order_by('-sale_date')[:5]

        return [
            {
                'id': sale.id,
                'customer': sale.customer.name if sale.customer else 'Walk-in',
                'amount': float(sale.final_amount),
                'time': sale.sale_date.strftime('%H:%M')
            }
            for sale in recent_sales
        ]

    def _get_sales_data_for_range(self, date_from, date_to):
        """Get sales data for the specified date range (for reports)"""
        from sales.models import Sale
        from payments.models import Payment

        sales = Sale.objects.filter(
            sale_date__date__range=[date_from, date_to]
        ).annotate(
            date=TruncDate('sale_date')
        ).values('date').annotate(
            total_sales=Sum('final_amount'),
            transaction_count=Count('id')
        ).order_by('date')

        # Get payment method breakdown for the entire range
        payments = Payment.objects.filter(
            sale__sale_date__date__range=[date_from, date_to],
            status='completed'
        ).values('payment_type').annotate(
            amount=Sum('amount')
        )

        payment_breakdown = {}
        for payment in payments:
            payment_breakdown[payment['payment_type']] = float(payment['amount'])

        result = []
        for sale in sales:
            result.append({
                'date': sale['date'].strftime('%Y-%m-%d'),
                'total_sales': float(sale['total_sales']),
                'cash_sales': float(payment_breakdown.get('cash', 0)),
                'card_sales': float(payment_breakdown.get('card', 0)),
                'mobile_sales': float(payment_breakdown.get('mpesa', 0)),
                'transactions': sale['transaction_count'],
                'gross_profit': float(sale['total_sales'] * Decimal('0.25')),
                'net_profit': float(sale['total_sales'] * Decimal('0.20'))
            })

        return result

    def _get_shift_sales_data(self, shift_id):
        """Get sales data for a specific shift"""
        from sales.models import Sale
        from payments.models import Payment

        # Get sales for the shift
        sales = Sale.objects.filter(shift_id=shift_id).annotate(
            date=TruncDate('sale_date')
        ).values('date').annotate(
            total_sales=Sum('total_amount'),
            transaction_count=Count('id')
        ).order_by('date')

        # Get payment method breakdown for the shift
        payments = Payment.objects.filter(
            sale__shift_id=shift_id,
            status='completed'
        ).values('payment_type').annotate(
            amount=Sum('amount')
        )

        payment_breakdown = {}
        for payment in payments:
            payment_breakdown[payment['payment_type']] = float(payment['amount'])

        # Get total sales and transactions for the shift
        total_sales = sum(float(sale['total_sales']) for sale in sales)
        total_transactions = sum(sale['transaction_count'] for sale in sales)

        # Get recent sales for the shift with payment info
        recent_sales = Sale.objects.filter(shift_id=shift_id).select_related('customer').prefetch_related('payment_set').order_by('-sale_date')[:10]

        result = {
            'total_sales': total_sales,
            'total_transactions': total_transactions,
            'average_sale': total_sales / total_transactions if total_transactions > 0 else 0,
            'today_sales': total_sales,  # Since it's for the shift
            'sales_by_payment_method': payment_breakdown,
            'recent_sales': [
                {
                    'id': sale.id,
                    'customer': sale.customer.name if sale.customer else 'Walk-in',
                    'total_amount': float(sale.total_amount),
                    'receipt_number': sale.receipt_number,
                    'created_at': sale.sale_date.isoformat(),
                    'payment_method': sale.payment_set.first().payment_type if sale.payment_set.exists() else 'N/A'
                }
                for sale in recent_sales
            ]
        }

        return result

class InventorySummaryView(generics.GenericAPIView):
    """Get inventory summary for dashboard and reports"""

    def get(self, request):
        # Check if detailed report data is requested
        if request.query_params.get('report') == 'detailed':
            # Return detailed inventory report data
            inventory_data = self._get_inventory_report_data()
            return Response(inventory_data)
        else:
            # Return dashboard summary data
            from inventory.models import Product

            total_items = Product.objects.aggregate(total=Sum('stock_quantity'))['total'] or 0
            total_value = Product.objects.aggregate(
                value=Sum(F('stock_quantity') * F('cost_price'))
            )['value'] or 0

            low_stock_items = Product.objects.filter(
                stock_quantity__lte=F('low_stock_threshold')
            ).count()

            out_of_stock_items = Product.objects.filter(stock_quantity=0).count()

            # Real stock movement data by category for chart
            from inventory.models import StockMovement
            from django.db.models.functions import TruncWeek

            # Get stock movements for the last 12 weeks
            twelve_weeks_ago = timezone.now() - timedelta(weeks=12)
            stock_movements = StockMovement.objects.filter(
                created_at__gte=twelve_weeks_ago
            ).annotate(
                week=TruncWeek('created_at')
            ).values('week', 'product__category__name').annotate(
                total_movement=Sum('quantity')
            ).order_by('week', 'product__category__name')

            # Group by week and category
            movement_by_week = {}
            categories = set()

            for movement in stock_movements:
                # Format week as "Week of YYYY-MM-DD"
                week_start = movement['week']
                week_key = f"Week of {week_start.strftime('%Y-%m-%d')}"
                category_name = movement['product__category__name'] or 'Uncategorized'
                categories.add(category_name)

                if week_key not in movement_by_week:
                    movement_by_week[week_key] = {}

                movement_by_week[week_key][category_name] = abs(movement['total_movement'])

            # Create chart data with all categories for the last 12 weeks
            stock_movement_data = []
            current_date = timezone.now().date()

            for i in range(12):
                week_start = current_date - timedelta(weeks=11-i)
                week_key = f"Week of {week_start.strftime('%Y-%m-%d')}"
                week_data = {'week': week_key}
                for category in categories:
                    week_data[category] = movement_by_week.get(week_key, {}).get(category, 0)
                stock_movement_data.append(week_data)

            data = {
                'total_items': total_items,
                'total_value': float(total_value),
                'low_stock_items': low_stock_items,
                'out_of_stock_items': out_of_stock_items,
                'stock_movement_data': stock_movement_data
            }

            serializer = InventorySummarySerializer(data)
            return Response(serializer.data)

    def _get_inventory_report_data(self):
        """Get detailed inventory data for reports"""
        from inventory.models import Product

        products = Product.objects.all().select_related('category')

        result = []
        for product in products:
            result.append({
                'product': product.name,
                'category': product.category.name if product.category else 'Uncategorized',
                'stock_level': product.stock_quantity,
                'sold_today': 0,  # Would be calculated from actual sales data
                'value': float(product.stock_quantity * product.selling_price),
            })

        return result

class CustomerSummaryView(generics.GenericAPIView):
    """Get customer summary for dashboard and reports"""

    def get(self, request):
        # Check if detailed report data is requested
        if request.query_params.get('report') == 'detailed':
            # Return detailed customer report data
            customer_data = self._get_customer_report_data()
            return Response(customer_data)
        else:
            # Return dashboard summary data
            from customers.models import Customer
            from sales.models import Sale

            total_customers = Customer.objects.count()
            # Count customers who have made purchases in the last 30 days
            thirty_days_ago = timezone.now().date() - timedelta(days=30)
            active_customers = Customer.objects.filter(
                sale__sale_date__date__gte=thirty_days_ago
            ).distinct().count()

            new_customers_today = Customer.objects.filter(
                created_at__date=timezone.now().date()
            ).count()

            # Top customers by purchase value
            top_customers = Sale.objects.values(
                'customer__name'
            ).annotate(
                total_spent=Sum('final_amount')
            ).order_by('-total_spent')[:5]

            data = {
                'total_customers': total_customers,
                'active_customers': active_customers,
                'new_customers_today': new_customers_today,
                'top_customers': [
                    {
                        'name': customer['customer__name'],
                        'total_spent': float(customer['total_spent'])
                    }
                    for customer in top_customers
                ]
            }

            serializer = CustomerSummarySerializer(data)
            return Response(serializer.data)

    def _get_customer_report_data(self):
        """Get detailed customer data for reports"""
        from customers.models import Customer
        from sales.models import Sale

        customers = Customer.objects.all()

        result = []
        for customer in customers:
            customer_sales = Sale.objects.filter(customer=customer)
            total_purchases = customer_sales.aggregate(total=Sum('final_amount'))['total'] or 0
            order_count = customer_sales.count()

            last_purchase = customer_sales.order_by('-sale_date').first()
            last_purchase_date = last_purchase.sale_date if last_purchase else None

            result.append({
                'name': customer.name,
                'phone': customer.phone,
                'total_purchases': float(total_purchases),
                'last_visit': last_purchase_date.strftime('%Y-%m-%d') if last_purchase_date else None,
                'loyalty_points': customer.loyalty_points,
            })

        return sorted(result, key=lambda x: x['total_purchases'], reverse=True)

class ShiftSummaryView(generics.GenericAPIView):
    """Get shift summary for dashboard and reports"""

    def get(self, request):
        # Check if detailed report data is requested
        if request.query_params.get('report') == 'detailed':
            # Return detailed shift report data
            shift_data = self._get_shift_report_data()
            return Response(shift_data)
        else:
            # Return dashboard summary data
            from shifts.models import Shift

            active_shifts = Shift.objects.filter(status='open').count()

            completed_shifts_today = Shift.objects.filter(
                end_time__date=timezone.now().date(),
                status='closed'
            ).count()

            total_shift_sales = Shift.objects.filter(
                end_time__date=timezone.now().date(),
                status='closed'
            ).aggregate(total=Sum('total_sales'))['total'] or 0

            data = {
                'active_shifts': active_shifts,
                'completed_shifts_today': completed_shifts_today,
                'total_shift_sales': float(total_shift_sales)
            }

            serializer = ShiftSummarySerializer(data)
            return Response(serializer.data)

    def _get_shift_report_data(self):
        """Get detailed shift data for reports"""
        from shifts.models import Shift

        shifts = Shift.objects.filter(status='closed').select_related('cashier__user').order_by('-end_time')

        result = []
        for shift in shifts:
            result.append({
                'cashier': shift.cashier.user.username,
                'shift_date': shift.end_time.date().strftime('%Y-%m-%d'),
                'start_time': shift.start_time.strftime('%H:%M') if shift.start_time else 'N/A',
                'end_time': shift.end_time.strftime('%H:%M') if shift.end_time else 'N/A',
                'opening_balance': float(shift.opening_balance),
                'closing_balance': float(shift.closing_balance),
                'total_sales': float(shift.total_sales),
                'discrepancy': float(shift.discrepancy) if shift.discrepancy else 0,
            })

        return result
