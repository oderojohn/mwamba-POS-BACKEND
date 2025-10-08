from rest_framework import viewsets, status
from rest_framework.generics import ListAPIView
from rest_framework.decorators import action
from rest_framework.response import Response
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.filters import SearchFilter, OrderingFilter
from django.db import models
from django.db.models import Sum, F
from .models import Category, Product, Batch, StockMovement, Supplier, Purchase, PriceHistory, SalesHistory
from .serializers import CategorySerializer, ProductSerializer, BatchSerializer, StockMovementSerializer, SupplierSerializer, PurchaseSerializer, PriceHistorySerializer, SalesHistorySerializer

class CategoryViewSet(viewsets.ModelViewSet):
    queryset = Category.objects.all()
    serializer_class = CategorySerializer
    filter_backends = [SearchFilter, OrderingFilter]
    search_fields = ['name', 'description']
    ordering_fields = ['name']
    ordering = ['name']

class ProductViewSet(viewsets.ModelViewSet):
    queryset = Product.objects.all()
    serializer_class = ProductSerializer
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['category', 'is_active']
    search_fields = ['sku', 'name', 'description']
    ordering_fields = ['name', 'created_at', 'selling_price']
    ordering = ['name']

class BatchViewSet(viewsets.ModelViewSet):
    queryset = Batch.objects.all()
    serializer_class = BatchSerializer
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['product', 'supplier', 'status']
    search_fields = ['batch_number', 'product__name']
    ordering_fields = ['purchase_date', 'expiry_date', 'received_date']
    ordering = ['-purchase_date']

    def update(self, request, *args, **kwargs):
        """Override update to add debugging"""
        print(f"Batch update request data: {request.data}")
        try:
            response = super().update(request, *args, **kwargs)
            print(f"Batch update successful: {response.data}")
            return response
        except Exception as e:
            print(f"Batch update error: {str(e)}")
            print(f"Error type: {type(e)}")
            if hasattr(e, 'detail'):
                print(f"Error detail: {e.detail}")
            raise

    @action(detail=True, methods=['post'])
    def receive(self, request, pk=None):
        """Mark batch as received and add to stock"""
        batch = self.get_object()

        if batch.status == 'received':
            return Response({'message': 'Batch already received'}, status=status.HTTP_400_BAD_REQUEST)

        # Get receiving data from request
        actual_quantity = request.data.get('actual_quantity', batch.quantity)
        cost_price = request.data.get('cost_price')
        received_date = request.data.get('received_date')
        notes = request.data.get('notes', '')
        condition = request.data.get('condition', 'good')

        # Validate actual quantity
        if actual_quantity > batch.quantity:
            return Response(
                {'error': 'Received quantity cannot exceed ordered quantity'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Update batch with receiving data
        if cost_price is not None:
            batch.cost_price = float(cost_price)
        if received_date:
            batch.received_date = received_date
        batch.save()

        # Create stock movement with actual received quantity
        batch.receive_batch(actual_quantity)

        # Create receiving log entry
        from .models import StockMovement
        StockMovement.objects.create(
            product=batch.product,
            movement_type='in',
            quantity=actual_quantity,
            reason=f'Batch {batch.batch_number} received - {condition} condition. Notes: {notes}',
            user=request.user.userprofile if hasattr(request.user, 'userprofile') else None
        )

        serializer = self.get_serializer(batch)
        return Response({
            'message': f'Batch {batch.batch_number} received successfully',
            'batch': serializer.data,
            'received_quantity': actual_quantity,
            'condition': condition
        })

class StockMovementViewSet(viewsets.ModelViewSet):
    queryset = StockMovement.objects.all()
    serializer_class = StockMovementSerializer
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['movement_type', 'product']
    search_fields = ['product__name', 'reason']
    ordering_fields = ['created_at']
    ordering = ['-created_at']

class LowStockView(ListAPIView):
    serializer_class = ProductSerializer
    def get_queryset(self):
        return Product.objects.filter(stock_quantity__lte=models.F('low_stock_threshold'))

class SupplierViewSet(viewsets.ModelViewSet):
    queryset = Supplier.objects.all()
    serializer_class = SupplierSerializer
    filter_backends = [SearchFilter, OrderingFilter]
    search_fields = ['name', 'contact_person', 'phone', 'email']
    ordering_fields = ['name']
    ordering = ['name']

    @action(detail=True, methods=['get'])
    def products(self, request, pk=None):
        supplier = self.get_object()
        products = Product.objects.filter(purchase__supplier=supplier).distinct()
        serializer = ProductSerializer(products, many=True)
        return Response(serializer.data)

class PurchaseViewSet(viewsets.ModelViewSet):
    queryset = Purchase.objects.all()
    serializer_class = PurchaseSerializer
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['supplier', 'product']
    search_fields = ['product__name', 'batch_number']
    ordering_fields = ['purchase_date']
    ordering = ['-purchase_date']

class PriceHistoryViewSet(viewsets.ModelViewSet):
    queryset = PriceHistory.objects.all()
    serializer_class = PriceHistorySerializer
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['supplier', 'product']
    search_fields = ['product__name', 'supplier__name']
    ordering_fields = ['date']
    ordering = ['-date']

    @action(detail=False, methods=['get'], url_path=r'product/(?P<product_id>\d+)')
    def by_product(self, request, product_id=None):
        histories = self.queryset.filter(product_id=product_id)
        serializer = self.get_serializer(histories, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'], url_path=r'supplier/(?P<supplier_id>\d+)')
    def by_supplier(self, request, supplier_id=None):
        histories = self.queryset.filter(supplier_id=supplier_id)
        serializer = self.get_serializer(histories, many=True)
        return Response(serializer.data)

class SalesHistoryViewSet(viewsets.ModelViewSet):
    queryset = SalesHistory.objects.all()
    serializer_class = SalesHistorySerializer
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['product', 'customer']
    search_fields = ['product__name', 'receipt_number']
    ordering_fields = ['sale_date']
    ordering = ['-sale_date']

    @action(detail=False, methods=['get'], url_path=r'product/(?P<product_id>\d+)')
    def by_product(self, request, product_id=None):
        histories = self.queryset.filter(product_id=product_id)
        serializer = self.get_serializer(histories, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'], url_path=r'customer/(?P<customer_id>\d+)')
    def by_customer(self, request, customer_id=None):
        histories = self.queryset.filter(customer_id=customer_id)
        serializer = self.get_serializer(histories, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'], url_path='date')
    def by_date_range(self, request):
        from_date = request.query_params.get('from')
        to_date = request.query_params.get('to')
        if from_date and to_date:
            histories = self.queryset.filter(sale_date__date__range=[from_date, to_date])
        else:
            histories = self.queryset
        serializer = self.get_serializer(histories, many=True)
        return Response(serializer.data)

# Report Views
class StockReportView(ListAPIView):
    def get(self, request):
        products = Product.objects.all()
        data = []
        for product in products:
            data.append({
                'product': product.name,
                'sku': product.sku,
                'stock_quantity': product.stock_quantity,
                'low_stock_threshold': product.low_stock_threshold,
                'is_low_stock': product.is_low_stock,
            })
        return Response(data)

class PurchaseReportView(ListAPIView):
    def get(self, request):
        purchases = Purchase.objects.all()
        data = purchases.values('product__name').annotate(
            total_quantity=Sum('quantity'),
            total_cost=Sum(F('quantity') * F('unit_price'))
        ).order_by('product__name')
        return Response(list(data))

class SupplierPerformanceView(ListAPIView):
    def get(self, request):
        suppliers = Supplier.objects.all()
        data = []
        for supplier in suppliers:
            purchases = Purchase.objects.filter(supplier=supplier)
            total_purchases = purchases.count()
            total_value = purchases.aggregate(total=Sum(F('quantity') * F('unit_price')))['total'] or 0
            data.append({
                'supplier': supplier.name,
                'total_purchases': total_purchases,
                'total_value': total_value,
            })
        return Response(data)

class InventoryValuationView(ListAPIView):
    def get(self, request):
        products = Product.objects.all()
        total_valuation = sum(product.stock_quantity * product.cost_price for product in products)
        data = {
            'total_inventory_valuation': total_valuation,
            'products': [
                {
                    'product': product.name,
                    'valuation': product.stock_quantity * product.cost_price,
                } for product in products
            ]
        }
        return Response(data)

# Batch expiry and alerts
class ExpiringBatchesView(ListAPIView):
    """Get batches that are expiring soon"""
    def get_queryset(self):
        from django.utils import timezone
        today = timezone.now().date()
        # Batches expiring within 30 days
        return Batch.objects.filter(
            expiry_date__gte=today,
            expiry_date__lte=today + timezone.timedelta(days=30),
            status='received',
            quantity__gt=0
        ).order_by('expiry_date')

    def get(self, request):
        batches = self.get_queryset()
        data = []
        for batch in batches:
            days_until_expiry = (batch.expiry_date - timezone.now().date()).days
            data.append({
                'id': batch.id,
                'batch_number': batch.batch_number,
                'product_name': batch.product.name,
                'supplier_name': batch.supplier.name if batch.supplier else 'N/A',
                'quantity': batch.quantity,
                'expiry_date': batch.expiry_date,
                'days_until_expiry': days_until_expiry,
                'urgency': 'critical' if days_until_expiry <= 7 else 'warning' if days_until_expiry <= 14 else 'info'
            })
        return Response(data)

class ExpiredBatchesView(ListAPIView):
    """Get batches that have expired"""
    def get_queryset(self):
        from django.utils import timezone
        return Batch.objects.filter(
            expiry_date__lt=timezone.now().date(),
            status='received',
            quantity__gt=0
        ).order_by('expiry_date')

    def get(self, request):
        batches = self.get_queryset()
        data = []
        for batch in batches:
            days_expired = (timezone.now().date() - batch.expiry_date).days
            data.append({
                'id': batch.id,
                'batch_number': batch.batch_number,
                'product_name': batch.product.name,
                'supplier_name': batch.supplier.name if batch.supplier else 'N/A',
                'quantity': batch.quantity,
                'expiry_date': batch.expiry_date,
                'days_expired': days_expired
            })
        return Response(data)

# Supplier performance analytics
class SupplierPerformanceView(ListAPIView):
    def get(self, request):
        from django.db.models import Sum, Count, Avg
        from django.utils import timezone
        from decimal import Decimal

        suppliers = Supplier.objects.all()
        data = []

        for supplier in suppliers:
            # Get all batches from this supplier
            batches = Batch.objects.filter(supplier=supplier, status='received')

            # Calculate metrics
            total_batches = batches.count()
            total_quantity = batches.aggregate(Sum('quantity'))['quantity__sum'] or 0
            total_value = sum(batch.quantity * batch.cost_price for batch in batches)

            # On-time delivery (batches received on or before expected date)
            on_time_deliveries = 0
            if hasattr(supplier, 'purchaseorder_set'):
                orders = supplier.purchaseorder_set.all()
                for order in orders:
                    if order.status == 'received':
                        expected_date = order.expected_delivery_date
                        if expected_date:
                            # Check if any batch from this order was received on time
                            order_batches = Batch.objects.filter(
                                supplier=supplier,
                                purchase_order_item__purchase_order=order
                            )
                            if order_batches.exists():
                                received_dates = [b.received_date for b in order_batches if b.received_date]
                                if received_dates and max(received_dates) <= expected_date:
                                    on_time_deliveries += 1

            # Quality metrics (expired batches as percentage)
            expired_batches = batches.filter(expiry_date__lt=timezone.now().date()).count()
            quality_score = ((total_batches - expired_batches) / total_batches * 100) if total_batches > 0 else 100

            data.append({
                'supplier': supplier.name,
                'total_batches': total_batches,
                'total_quantity': total_quantity,
                'total_value': float(total_value),
                'on_time_delivery_rate': (on_time_deliveries / max(1, orders.count())) * 100 if hasattr(supplier, 'purchaseorder_set') else 0,
                'quality_score': quality_score,
                'expired_batches': expired_batches
            })

        return Response(data)

# Product recall functionality
class ProductRecallViewSet(viewsets.ModelViewSet):
    queryset = Batch.objects.all()
    serializer_class = BatchSerializer

    @action(detail=True, methods=['post'])
    def recall(self, request, pk=None):
        """Mark a batch for recall and remove from stock"""
        batch = self.get_object()

        if batch.status == 'expired':
            return Response({'message': 'Batch is already expired'}, status=status.HTTP_400_BAD_REQUEST)

        # Mark batch as expired (recall)
        batch.status = 'expired'
        batch.save()

        # Remove from product stock
        batch.product.stock_quantity -= batch.quantity
        batch.product.save()

        # Create stock movement record
        StockMovement.objects.create(
            product=batch.product,
            movement_type='adjustment',
            quantity=-batch.quantity,
            reason=f'Product recall - Batch {batch.batch_number}',
            user=request.user.userprofile if hasattr(request.user, 'userprofile') else None
        )

        return Response({'message': f'Batch {batch.batch_number} recalled successfully'})

# Profit analysis views
class ProfitAnalysisView(ListAPIView):
    def get(self, request):
        from django.db.models import Sum
        from django.utils import timezone

        # Get date range from query params
        start_date = request.query_params.get('start_date')
        end_date = request.query_params.get('end_date')

        if not start_date or not end_date:
            # Default to current month
            today = timezone.now().date()
            start_date = today.replace(day=1)
            end_date = today

        # Get sales history with profit data
        sales = SalesHistory.objects.filter(
            sale_date__date__range=[start_date, end_date]
        )

        total_sales = sales.aggregate(
            total_revenue=Sum('total_price'),
            total_cost=Sum('cost_price') * Sum('quantity'),
            total_profit=Sum('profit')
        )

        # Product-wise profit analysis
        product_profits = sales.values('product__name').annotate(
            revenue=Sum('total_price'),
            cost=Sum('cost_price') * Sum('quantity'),
            profit=Sum('profit'),
            quantity_sold=Sum('quantity')
        ).order_by('-profit')

        data = {
            'period': {
                'start_date': start_date,
                'end_date': end_date
            },
            'summary': {
                'total_revenue': float(total_sales['total_revenue'] or 0),
                'total_cost': float(total_sales['total_cost'] or 0),
                'total_profit': float(total_sales['total_profit'] or 0),
                'profit_margin': (total_sales['total_profit'] or 0) / (total_sales['total_revenue'] or 1) * 100
            },
            'product_analysis': list(product_profits)
        }

        return Response(data)
