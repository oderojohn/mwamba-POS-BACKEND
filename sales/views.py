from rest_framework import viewsets, status, serializers
from rest_framework.response import Response
from rest_framework.decorators import action
from myshop.pagination import StandardResultsPagination
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.filters import SearchFilter, OrderingFilter
from django.utils import timezone
from django.db import transaction
from django.conf import settings
from django.db.models import Q, Prefetch
from .models import Cart, CartItem, Sale, SaleItem, Return, ReturnItem, Invoice, InvoiceItem, AuditLog
from chits.models import Chit
from .serializers import CartSerializer, CartItemSerializer, SaleSerializer, SaleItemSerializer, ReturnSerializer, InvoiceSerializer, InvoiceItemSerializer, AuditLogSerializer
from inventory.models import Product, StockMovement, SalesHistory
from shifts.models import Shift
from payments.models import Payment
from .services import sales_service, audit_service, checkout
import logging
logger = logging.getLogger(__name__)
from .services.receipt_number_service import get_next_receipt_number, get_next_return_receipt_number

class CartViewSet(viewsets.ModelViewSet):
    queryset = Cart.objects.all()
    serializer_class = CartSerializer

class CartItemViewSet(viewsets.ModelViewSet):
    queryset = CartItem.objects.all()
    serializer_class = CartItemSerializer

class SaleViewSet(viewsets.ModelViewSet):
    queryset = Sale.objects.select_related(
        'customer', 'voided_by__user', 'edited_by__user'
    ).prefetch_related(
        Prefetch('saleitem_set', queryset=SaleItem.objects.select_related('product')),
        'payment_set',
    ).all()
    serializer_class = SaleSerializer
    pagination_class = StandardResultsPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['receipt_number', 'sale_type', 'voided', 'customer', 'shift']
    search_fields = ['receipt_number', 'customer__name', 'customer__phone']
    ordering_fields = ['-sale_date', 'sale_date', 'total_amount']
    ordering = ['-sale_date']

    def get_queryset(self):
        queryset = super().get_queryset()
        
        # Handle shift_id filter explicitly - also validate user if provided
        shift_id = self.request.query_params.get('shift_id')
        user_id = self.request.query_params.get('user_id')
        
        if shift_id:
            # If user_id is provided, verify the shift belongs to this user
            if user_id:
                queryset = queryset.filter(shift_id=shift_id, shift__cashier__user_id=user_id)
            else:
                queryset = queryset.filter(shift_id=shift_id)
        elif user_id:
            # If only user_id is provided, filter by user
            queryset = queryset.filter(shift__cashier__user_id=user_id)
        
        return queryset

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        print(f"[DEBUG] Sale retrieve - ID: {instance.id}, Receipt: {instance.receipt_number}")
        
        # Debug: Check items using different approaches
        print(f"[DEBUG] Using prefetch: {list(instance.saleitem_set.all())}")
        print(f"[DEBUG] Items count via prefetch: {instance.saleitem_set.count()}")
        
        # Try direct query
        direct_items = SaleItem.objects.filter(sale=instance)
        print(f"[DEBUG] Direct query items: {list(direct_items)}")
        print(f"[DEBUG] Direct query count: {direct_items.count()}")
        
        for item in instance.saleitem_set.all():
            print(f"[DEBUG]   Item: {item.id} - {item.product.name if item.product else 'No product'} - Qty: {item.quantity} - Price: {item.unit_price}")
        
        serializer = self.get_serializer(instance)
        data = serializer.data
        print(f"[DEBUG] Serialized data keys: {data.keys()}")
        print(f"[DEBUG] Serialized items: {data.get('items', 'NO ITEMS FIELD')}")
        return Response(data)

    @action(detail=False, methods=['get'])
    def held_orders(self, request):
        """Get all held orders for the current cashier's shift"""
        cashier = None
        if hasattr(request.user, 'userprofile'):
            cashier = request.user.userprofile

        # If no cashier/user, return empty list instead of error
        if not cashier:
            return Response([])

        try:
            # Check if shift_id is provided
            shift_id = request.query_params.get('shift_id')
            if shift_id:
                # Filter by specific shift ID using the Shift model
                from shifts.models import Shift
                try:
                    shift = Shift.objects.get(id=shift_id, cashier=cashier)
                    # Cart doesn't have shift field, filter by cashier only for this shift
                    held_carts = Cart.objects.filter(
                        cashier=cashier,
                        status='held'
                    ).prefetch_related('cartitem_set__product').order_by('-created_at')
                except Shift.DoesNotExist:
                    held_carts = Cart.objects.none()
            else:
                # Default behavior - get held orders for current shift
                held_carts = sales_service.get_held_orders(cashier)
        except ValueError as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )

        serializer = CartSerializer(held_carts, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'])
    def by_receipt(self, request):
        """Search sales by receipt number (case-insensitive, exact or partial match)"""
        receipt_number = request.query_params.get('receipt_number', '').strip()
        
        if not receipt_number:
            return Response(
                {'error': 'Receipt number is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Try exact match first, then case-insensitive partial match
        sale = Sale.objects.filter(
            Q(receipt_number__iexact=receipt_number) | 
            Q(receipt_number__icontains=receipt_number)
        ).first()
        
        if not sale:
            return Response(
                {'error': 'Transaction not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        serializer = SaleSerializer(sale)
        return Response(serializer.data)

    @action(detail=True, methods=['post'])
    def complete_held_order(self, request, pk=None):
        """Complete a held order (server pricing, idempotent per cart)."""
        return self._run_checkout(request, held_cart_id=pk)

    @action(detail=True, methods=['post'])
    def void_held_order(self, request, pk=None):
        """Void/cancel a held order with a reason"""
        try:
            cart = Cart.objects.get(id=pk, status='held')
        except Cart.DoesNotExist:
            return Response(
                {'error': 'Held order not found'},
                status=status.HTTP_404_NOT_FOUND
            )

        # Check if cashier has permission to void this order
        cashier = None
        if hasattr(request.user, 'userprofile'):
            cashier = request.user.userprofile

        if not cashier or cart.cashier != cashier:
            return Response(
                {'error': 'Unauthorized to void this order'},
                status=status.HTTP_403_FORBIDDEN
            )

        void_reason = request.data.get('void_reason', '').strip()
        if not void_reason:
            return Response(
                {'error': 'Void reason is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Void the order
        sales_service.void_held_order(cart, void_reason, cashier)

        return Response({
            'message': 'Held order voided successfully',
            'void_reason': void_reason
        }, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'])
    def update_held_order(self, request, pk=None):
        """Update a held order (add/remove items)"""
        try:
            cart = Cart.objects.get(id=pk, status='held')
        except Cart.DoesNotExist:
            return Response(
                {'error': 'Held order not found'},
                status=status.HTTP_404_NOT_FOUND
            )

        # Check if cashier has permission to update this order
        cashier = None
        if hasattr(request.user, 'userprofile'):
            cashier = request.user.userprofile

        # Allow update if user is admin or is the cashier who created the order
        if not cashier or (cart.cashier and cart.cashier != cashier):
            # Check if user is admin
            if not request.user.is_staff and not request.user.is_superuser:
                return Response(
                    {'error': 'Unauthorized to update this order'},
                    status=status.HTTP_403_FORBIDDEN
                )

        # Get items to add/remove
        items_to_add = request.data.get('items_to_add', [])
        items_to_remove = request.data.get('items_to_remove', [])  # List of item IDs
        update_quantities = request.data.get('update_quantities', {})  # {item_id: new_quantity}

        try:
            # Remove items if specified
            if items_to_remove:
                # Convert to integers
                item_ids = [int(x) for x in items_to_remove if str(x).isdigit()]
                cart.cartitem_set.filter(id__in=item_ids).delete()

            # Update quantities if specified
            if update_quantities:
                for item_id, new_qty in update_quantities.items():
                    try:
                        cart_item = cart.cartitem_set.get(id=int(item_id))
                        if int(new_qty) <= 0:
                            cart_item.delete()
                        else:
                            cart_item.quantity = int(new_qty)
                            cart_item.save()
                    except (CartItem.DoesNotExist, ValueError):
                        pass

            # Add new items if specified
            if items_to_add:
                for item_data in items_to_add:
                    product_id = item_data.get('product')
                    quantity = int(item_data.get('quantity', 1))
                    unit_price = item_data.get('unit_price')

                    try:
                        product = Product.objects.get(id=int(product_id))
                        # Check if product already exists in cart
                        existing_item = cart.cartitem_set.filter(product=product).first()
                        if existing_item:
                            # Update quantity
                            existing_item.quantity += quantity
                            if unit_price:
                                existing_item.unit_price = float(unit_price)
                            existing_item.save()
                        else:
                            # Add new item
                            CartItem.objects.create(
                                cart=cart,
                                product=product,
                                quantity=quantity,
                                unit_price=float(unit_price) if unit_price else product.selling_price
                            )
                    except (Product.DoesNotExist, ValueError):
                        pass

            # Reload cart to get updated items
            cart_items = cart.cartitem_set.select_related('product')
            updated_items = []
            for item in cart_items:
                updated_items.append({
                    'id': item.id,
                    'product': item.product.id,
                    'product_name': item.product.name,
                    'quantity': item.quantity,
                    'unit_price': str(item.unit_price),
                    'total': str(item.unit_price * item.quantity)
                })

            # Calculate new total (convert to float first)
            new_total = sum(float(item['unit_price']) * item['quantity'] for item in updated_items)

            return Response({
                'message': 'Held order updated successfully',
                'items': updated_items,
                'total': str(new_total)
            }, status=status.HTTP_200_OK)

        except Exception as e:
            return Response(
                {'error': f'Failed to update held order: {str(e)}'},
                status=status.HTTP_400_BAD_REQUEST
            )

    @action(detail=True, methods=['post'])
    def void_sale(self, request, pk=None):
        """Void a sale. Cashiers: own sale in their open shift. Managers/admins: any sale."""
        return self._run_void(request, pk, require_manager=False)

    @action(detail=True, methods=['post'])
    def admin_void_sale(self, request, pk=None):
        """Admin/manager void of a completed sale."""
        return self._run_void(request, pk, require_manager=True)

    @action(detail=True, methods=['post'])
    def void_items(self, request, pk=None):
        """Disabled: editing completed sales left stock, payments and shift totals inconsistent.
        Void the sale (and ring it up again) or process a return instead."""
        return Response(
            {'error': 'Editing completed sales is no longer supported. Void the sale or process a return instead.'},
            status=status.HTTP_410_GONE,
        )

    @action(detail=False, methods=['get'], permission_classes=[])
    def sales_by_user(self, request):
        """View sales by user (requires admin/manager role)"""
        # Check admin permissions
        if not hasattr(request.user, 'userprofile') or request.user.userprofile.role not in ['admin', 'manager']:
            return Response(
                {'error': 'Admin or Manager role required for this action'},
                status=status.HTTP_403_FORBIDDEN
            )

        user_id = request.query_params.get('user_id')
        date_from = request.query_params.get('date_from')
        date_to = request.query_params.get('date_to')

        queryset = Sale.objects.filter(voided=False)

        if user_id:
            queryset = queryset.filter(shift__cashier__user_id=user_id)

        if date_from:
            queryset = queryset.filter(sale_date__gte=date_from)

        if date_to:
            queryset = queryset.filter(sale_date__lte=date_to)

        # Group by user
        sales_by_user = {}
        for sale in queryset.select_related('shift__cashier__user'):
            user = sale.shift.cashier.user if sale.shift and sale.shift.cashier else None
            if user:
                user_key = f"{user.username} ({user.id})"
                if user_key not in sales_by_user:
                    sales_by_user[user_key] = {
                        'user_id': user.id,
                        'username': user.username,
                        'total_sales': 0,
                        'total_amount': 0,
                        'sales_count': 0,
                        'sales': []
                    }

                sales_by_user[user_key]['total_sales'] += 1
                sales_by_user[user_key]['total_amount'] += float(sale.final_amount)
                sales_by_user[user_key]['sales_count'] += 1
                sales_by_user[user_key]['sales'].append({
                    'id': sale.id,
                    'receipt_number': sale.receipt_number,
                    'final_amount': sale.final_amount,
                    'sale_date': sale.sale_date
                })

        return Response({
            'sales_by_user': sales_by_user,
            'total_users': len(sales_by_user),
            'filters': {
                'user_id': user_id,
                'date_from': date_from,
                'date_to': date_to
            }
        })

    @action(detail=True, methods=['patch'])
    def admin_edit_sale(self, request, pk=None):
        """Disabled: editing completed sales left stock, payments and shift totals inconsistent.
        Void the sale (and ring it up again) or process a return instead."""
        return Response(
            {'error': 'Editing completed sales is no longer supported. Void the sale or process a return instead.'},
            status=status.HTTP_410_GONE,
        )

    @action(detail=False, methods=['get'], permission_classes=[])
    def sales_by_date(self, request):
        """View all sales by date (Admin/Manager only)"""
        if not hasattr(request.user, 'userprofile') or request.user.userprofile.role not in ['admin', 'manager']:
            return Response(
                {'error': 'Admin or Manager role required'},
                status=status.HTTP_403_FORBIDDEN
            )

        date_from = request.query_params.get('date_from')
        date_to = request.query_params.get('date_to')
        status_filter = request.query_params.get('status')  # completed, voided, held

        queryset = Sale.objects.select_related('customer', 'shift__cashier__user')

        # Apply branch filter for managers
        if request.user.userprofile.role == 'manager' and request.user.userprofile.branch:
            queryset = queryset.filter(shift__cashier__branch=request.user.userprofile.branch)

        # Date filters
        if date_from:
            queryset = queryset.filter(sale_date__date__gte=date_from)
        if date_to:
            queryset = queryset.filter(sale_date__date__lte=date_to)

        # Status filter
        if status_filter:
            if status_filter == 'voided':
                queryset = queryset.filter(voided=True)
            elif status_filter == 'held':
                queryset = queryset.filter(cart__status='held')
            elif status_filter == 'completed':
                queryset = queryset.filter(voided=False, cart__status='closed')

        # Group by date
        sales_by_date = {}
        for sale in queryset.order_by('-sale_date'):
            date_key = sale.sale_date.date().isoformat()
            if date_key not in sales_by_date:
                sales_by_date[date_key] = {
                    'date': date_key,
                    'total_sales': 0,
                    'total_amount': 0,
                    'voided_count': 0,
                    'completed_count': 0,
                    'sales': []
                }

            sales_by_date[date_key]['total_sales'] += 1
            sales_by_date[date_key]['total_amount'] += float(sale.final_amount)

            if sale.voided:
                sales_by_date[date_key]['voided_count'] += 1
            else:
                sales_by_date[date_key]['completed_count'] += 1

            sales_by_date[date_key]['sales'].append({
                'id': sale.id,
                'receipt_number': sale.receipt_number,
                'customer': sale.customer.name if sale.customer else None,
                'cashier': sale.shift.cashier.user.username if sale.shift and sale.shift.cashier else None,
                'final_amount': sale.final_amount,
                'voided': sale.voided,
                'sale_date': sale.sale_date,
                'item_count': sale.saleitem_set.count()
            })

        return Response({
            'sales_by_date': sales_by_date,
            'total_dates': len(sales_by_date),
            'filters': {
                'date_from': date_from,
                'date_to': date_to,
                'status': status_filter
            }
        })

    @action(detail=False, methods=['get'], permission_classes=[])
    def held_orders_admin(self, request):
        """View all held orders for admin/manager review"""
        if not hasattr(request.user, 'userprofile') or request.user.userprofile.role not in ['admin', 'manager']:
            return Response(
                {'error': 'Admin or Manager role required'},
                status=status.HTTP_403_FORBIDDEN
            )

        queryset = Cart.objects.filter(status='held').select_related('customer', 'cashier__user')

        # Branch filter for managers
        if request.user.userprofile.role == 'manager' and request.user.userprofile.branch:
            queryset = queryset.filter(cashier__branch=request.user.userprofile.branch)

        held_orders = []
        for cart in queryset.order_by('-created_at'):
            items = cart.cartitem_set.select_related('product')
            held_orders.append({
                'id': cart.id,
                'customer': cart.customer.name if cart.customer else None,
                'cashier': cart.cashier.user.username if cart.cashier else None,
                'created_at': cart.created_at,
                'item_count': items.count(),
                'total_quantity': sum(item.quantity for item in items),
                'estimated_total': sum(float(item.unit_price) * item.quantity for item in items),
                'items': [{
                    'product': item.product.name,
                    'quantity': item.quantity,
                    'unit_price': item.unit_price,
                    'total': float(item.unit_price) * item.quantity
                } for item in items]
            })

        return Response({
            'held_orders': held_orders,
            'total_count': len(held_orders)
        })

    @action(detail=False, methods=['get'], permission_classes=[])
    def voided_orders(self, request):
        """View all voided sales and orders (Admin/Manager only)"""
        if not hasattr(request.user, 'userprofile') or request.user.userprofile.role not in ['admin', 'manager']:
            return Response(
                {'error': 'Admin or Manager role required'},
                status=status.HTTP_403_FORBIDDEN
            )

        date_from = request.query_params.get('date_from')
        date_to = request.query_params.get('date_to')

        # Voided sales
        voided_sales_queryset = Sale.objects.filter(voided=True).select_related('customer', 'shift__cashier__user', 'voided_by__user')

        # Branch filter for managers
        if request.user.userprofile.role == 'manager' and request.user.userprofile.branch:
            voided_sales_queryset = voided_sales_queryset.filter(shift__cashier__branch=request.user.userprofile.branch)

        # Date filters
        if date_from:
            voided_sales_queryset = voided_sales_queryset.filter(sale_date__date__gte=date_from)
        if date_to:
            voided_sales_queryset = voided_sales_queryset.filter(sale_date__date__lte=date_to)

        voided_sales = []
        for sale in voided_sales_queryset.order_by('-voided_at'):
            items = sale.saleitem_set.select_related('product')
            voided_sales.append({
                'id': sale.id,
                'type': 'sale',
                'receipt_number': sale.receipt_number,
                'customer': sale.customer.name if sale.customer else None,
                'cashier': sale.shift.cashier.user.username if sale.shift and sale.shift.cashier else None,
                'void_reason': sale.void_reason,
                'voided_by': sale.voided_by.user.username if sale.voided_by else None,
                'voided_at': sale.voided_at,
                'original_amount': sale.final_amount,
                'item_count': items.count(),
                'items': [{
                    'product': item.product.name,
                    'quantity': item.quantity,
                    'unit_price': item.unit_price
                } for item in items]
            })

        # Voided carts (held orders that were voided)
        voided_carts_queryset = Cart.objects.filter(status='voided').select_related('customer', 'cashier__user')

        if request.user.userprofile.role == 'manager' and request.user.userprofile.branch:
            voided_carts_queryset = voided_carts_queryset.filter(cashier__branch=request.user.userprofile.branch)

        voided_carts = []
        for cart in voided_carts_queryset.order_by('-created_at'):
            items = cart.cartitem_set.select_related('product')
            voided_carts.append({
                'id': cart.id,
                'type': 'cart',
                'customer': cart.customer.name if cart.customer else None,
                'cashier': cart.cashier.user.username if cart.cashier else None,
                'void_reason': cart.void_reason,
                'voided_at': cart.created_at,  # No specific void timestamp for carts
                'estimated_amount': sum(float(item.unit_price) * item.quantity for item in items),
                'item_count': items.count(),
                'items': [{
                    'product': item.product.name,
                    'quantity': item.quantity,
                    'unit_price': item.unit_price
                } for item in items]
            })

        return Response({
            'voided_sales': voided_sales,
            'voided_carts': voided_carts,
            'total_voided': len(voided_sales) + len(voided_carts),
            'filters': {
                'date_from': date_from,
                'date_to': date_to
            }
        })

    @action(detail=True, methods=['get'], permission_classes=[])
    def transaction_details(self, request, pk=None):
        """Get detailed transaction information including all items (Admin/Manager only)"""
        if not hasattr(request.user, 'userprofile') or request.user.userprofile.role not in ['admin', 'manager']:
            return Response(
                {'error': 'Admin or Manager role required'},
                status=status.HTTP_403_FORBIDDEN
            )

        try:
            sale = Sale.objects.select_related('customer', 'shift__cashier__user', 'cart').get(id=pk)
        except Sale.DoesNotExist:
            return Response(
                {'error': 'Sale not found'},
                status=status.HTTP_404_NOT_FOUND
            )

        # Check branch access for managers
        if (request.user.userprofile.role == 'manager' and request.user.userprofile.branch and
            sale.shift and sale.shift.cashier and sale.shift.cashier.branch != request.user.userprofile.branch):
            return Response(
                {'error': 'Access denied - sale from different branch'},
                status=status.HTTP_403_FORBIDDEN
            )

        # Get sale items with product details
        items = sale.saleitem_set.select_related('product').order_by('product__name')

        # Get payment details
        payments = sale.payment_set.select_related('sale').order_by('created_at')

        # Get audit logs for this sale
        audit_logs = AuditLog.objects.filter(
            entity_type='Sale',
            entity_id=sale.id
        ).select_related('user__user').order_by('-timestamp')[:10]  # Last 10 operations

        transaction_data = {
            'sale': {
                'id': sale.id,
                'receipt_number': sale.receipt_number,
                'sale_date': sale.sale_date,
                'customer': sale.customer.name if sale.customer else None,
                'cashier': sale.shift.cashier.user.username if sale.shift and sale.shift.cashier else None,
                'sale_type': sale.sale_type,
                'voided': sale.voided,
                'void_reason': sale.void_reason,
                'voided_by': sale.voided_by.user.username if sale.voided_by else None,
                'voided_at': sale.voided_at,
            },
            'financials': {
                'subtotal': sale.total_amount,
                'tax_amount': sale.tax_amount,
                'discount_amount': sale.discount_amount,
                'final_amount': sale.final_amount,
            },
            'items': [{
                'id': item.id,
                'product': item.product.name,
                'product_id': item.product.id,
                'quantity': item.quantity,
                'unit_price': item.unit_price,
                'discount': item.discount,
                'line_total': (item.unit_price * item.quantity) - item.discount
            } for item in items],
            'payments': [{
                'id': payment.id,
                'payment_type': payment.payment_type,
                'amount': payment.amount,
                'status': payment.status,
                'mpesa_number': payment.mpesa_number,
                'created_at': payment.created_at
            } for payment in payments],
            'audit_trail': [{
                'id': log.id,
                'operation': log.operation,
                'description': log.description,
                'user': log.user.user.username if log.user else 'System',
                'timestamp': log.timestamp,
                'old_values': log.old_values,
                'new_values': log.new_values
            } for log in audit_logs]
        }

        return Response(transaction_data)

    @action(detail=True, methods=['patch'])
    def edit_transaction(self, request, pk=None):
        """Disabled: editing completed sales left stock, payments and shift totals inconsistent.
        Void the sale (and ring it up again) or process a return instead."""
        return Response(
            {'error': 'Editing completed sales is no longer supported. Void the sale or process a return instead.'},
            status=status.HTTP_410_GONE,
        )

    @action(detail=True, methods=['post'])
    def void_transaction(self, request, pk=None):
        """Admin/manager void of a whole transaction (same as admin_void_sale)."""
        return self._run_void(request, pk, require_manager=True)

    def create(self, request, *args, **kwargs):
        """Create a sale from cart data, or hold an order (hold_order=true)."""
        if request.data.get('hold_order'):
            return self._hold_order(request)
        return self._run_checkout(request)

    def update(self, request, *args, **kwargs):
        return Response({'error': 'Sales cannot be edited. Void the sale or process a return.'},
                        status=status.HTTP_405_METHOD_NOT_ALLOWED)

    partial_update = update

    def destroy(self, request, *args, **kwargs):
        return Response({'error': 'Sales cannot be deleted. Void the sale instead.'},
                        status=status.HTTP_405_METHOD_NOT_ALLOWED)

    # ---- helpers -------------------------------------------------------
    def _run_checkout(self, request, held_cart_id=None):
        from django.db import IntegrityError
        try:
            sale, created, extras = checkout.checkout(user=request.user, data=request.data,
                                                      held_cart_id=held_cart_id)
        except checkout.SaleError as e:
            return Response(e.as_response_data(), status=e.status)
        except IntegrityError:
            # two identical requests raced: return the one that won
            txn = request.data.get('client_txn_id')
            sale = Sale.objects.filter(client_txn_id=txn).first() if txn else None
            if not sale:
                logger.exception('Sale checkout integrity error')
                return Response({'error': 'Sale could not be saved. Please retry.'},
                                status=status.HTTP_409_CONFLICT)
            created, extras = False, checkout._extras(sale)
        data = dict(self.get_serializer(sale).data)
        data.update(extras)
        return Response(data, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)

    def _run_void(self, request, pk, require_manager):
        try:
            sale = checkout.void_sale(user=request.user, sale_id=pk,
                                      reason=request.data.get('reason') or request.data.get('void_reason'),
                                      require_manager=require_manager)
        except checkout.SaleError as e:
            return Response(e.as_response_data(), status=e.status)
        return Response({
            'message': 'Sale voided successfully - all items restocked',
            'sale_id': sale.id,
            'void_reason': sale.void_reason,
            'amount_refunded': str(sale.final_amount),
            'voided_by': request.user.username,
        }, status=status.HTTP_200_OK)

    def _hold_order(self, request):
        """Save a cart as a held order. No stock is taken until it is completed."""
        try:
            profile = checkout.get_profile(request.user)
            lines = checkout._parse_lines(request.data.get('items'))
        except checkout.SaleError as e:
            return Response(e.as_response_data(), status=e.status)
        if not Shift.objects.filter(cashier=profile, status='open').exists():
            return Response({'error': 'No active shift found. Please start a shift before processing sales.'},
                            status=status.HTTP_400_BAD_REQUEST)
        sale_type = request.data.get('sale_type') or 'retail'
        with transaction.atomic():
            cart = Cart.objects.create(cashier=profile, status='held')
            for product_id, qty, discount in lines:
                product = Product.objects.filter(id=product_id, is_active=True).first()
                if not product:
                    transaction.set_rollback(True)
                    return Response({'error': f'Product {product_id} not found or inactive'},
                                    status=status.HTTP_400_BAD_REQUEST)
                CartItem.objects.create(cart=cart, product=product, quantity=qty,
                                        unit_price=checkout._unit_price(product, sale_type), discount=discount)
        return Response({
            'id': cart.id, 'message': 'Order held successfully', 'cart_id': cart.id, 'status': 'held',
            'items': CartItemSerializer(cart.cartitem_set.all(), many=True).data,
        }, status=status.HTTP_201_CREATED)

class SaleItemViewSet(viewsets.ModelViewSet):
    queryset = SaleItem.objects.all()
    serializer_class = SaleItemSerializer

class ReturnViewSet(viewsets.ModelViewSet):
    queryset = Return.objects.select_related('sale', 'processed_by__user').prefetch_related(
        Prefetch('items', queryset=ReturnItem.objects.select_related('sale_item__product')),
        'exchange_items',
    )
    serializer_class = ReturnSerializer
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['shift']
    ordering_fields = ['-return_date', 'return_date', 'total_refund_amount']
    ordering = ['-return_date']
    pagination_class = StandardResultsPagination

    def get_queryset(self):
        """Prefetch related objects for better performance"""
        from django.db.models import Prefetch
        
        queryset = Return.objects.prefetch_related(
            Prefetch('items', queryset=ReturnItem.objects.select_related('sale_item__product')),
            'exchange_items__product',
            'return_codes',
            'sale',
            'sale__cart',
            'processed_by__user',
            'shift'
        ).select_related('sale__customer')
        
        # Handle shift_id filter explicitly - apply to ALL users including admins
        shift_id = self.request.query_params.get('shift_id')
        user_id = self.request.query_params.get('user_id')
        
        # If shift_id is provided, filter by it (applies to everyone)
        if shift_id:
            queryset = queryset.filter(shift_id=shift_id)
        # If user_id is provided, filter by user
        elif user_id:
            queryset = queryset.filter(shift__cashier__user_id=user_id)
        # For non-admin users without shift_id, filter by their returns
        elif not self.request.user.is_staff:
            if hasattr(self.request.user, 'userprofile'):
                try:
                    from users.models import UserProfile
                    user_profile = self.request.user.userprofile
                    queryset = queryset.filter(processed_by=user_profile)
                except:
                    pass
        # Admin users without shift_id see all returns
        
        return queryset

    def create(self, request, *args, **kwargs):
        """Create a return: restock, refund at the price paid, issue a return code."""
        try:
            return_record, code = checkout.create_return(user=request.user, data=request.data)
        except checkout.SaleError as e:
            return Response(e.as_response_data(), status=e.status)

        sale = return_record.sale
        lines = '\n'.join(
            f"- {ri.sale_item.product.name}: {ri.quantity} x {ri.sale_item.unit_price}"
            + (f" ({ri.reason})" if ri.reason else '')
            for ri in return_record.items.select_related('sale_item__product'))
        chit_content = (
            f"RETURN RECEIPT\n================\nReturn ID: {return_record.id}\n"
            f"Original Receipt: {sale.receipt_number}\n"
            f"Return Date: {return_record.return_date.strftime('%Y-%m-%d %H:%M')}\n\n"
            f"Returned Items:\n{lines}\n\nTotal Refund: {return_record.total_refund_amount}\n"
        )
        if code:
            chit_content += f"\n====================\nRETURN CODE: {code.code}\nUse this code for future purchases\n"
        Chit.objects.create(
            customer=sale.customer,
            customer_name=sale.customer.name if sale.customer else 'Walk-in Customer',
            amount=return_record.total_refund_amount, description=chit_content, status='closed',
        )
        data = dict(self.get_serializer(return_record).data)
        data['return_code'] = code.code if code else None
        data['refund_method'] = return_record.refund_method
        return Response(data, status=status.HTTP_201_CREATED)

class InvoiceViewSet(viewsets.ModelViewSet):
    queryset = Invoice.objects.select_related('customer', 'sale', 'created_by__user').prefetch_related('items')
    serializer_class = InvoiceSerializer
    pagination_class = StandardResultsPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['customer', 'status', 'sale']
    search_fields = ['invoice_number', 'customer__name', 'notes']
    ordering_fields = ['invoice_date', 'due_date', 'total_amount', 'created_at']
    ordering = ['-invoice_date']

    @action(detail=True, methods=['post'])
    def mark_paid(self, request, pk=None):
        """Mark invoice as paid"""
        invoice = self.get_object()
        invoice.status = 'paid'
        invoice.save()
        serializer = self.get_serializer(invoice)
        return Response(serializer.data)

    @action(detail=True, methods=['post'])
    def send_invoice(self, request, pk=None):
        """Mark invoice as sent"""
        invoice = self.get_object()
        invoice.status = 'sent'
        invoice.save()
        serializer = self.get_serializer(invoice)
        return Response(serializer.data)

    @action(detail=False, methods=['post'])
    def generate_from_sale(self, request):
        """Generate invoice from an existing sale"""
        sale_id = request.data.get('sale_id')
        customer_id = request.data.get('customer_id')
        due_date = request.data.get('due_date')

        try:
            sale = Sale.objects.get(id=sale_id)
        except Sale.DoesNotExist:
            return Response({'error': 'Sale not found'}, status=status.HTTP_404_NOT_FOUND)

        # Check if invoice already exists for this sale
        if hasattr(sale, 'invoice'):
            return Response({'error': 'Invoice already exists for this sale'}, status=status.HTTP_400_BAD_REQUEST)

        # Get customer (from sale or specified)
        customer = sale.customer
        if customer_id:
            from customers.models import Customer
            try:
                customer = Customer.objects.get(id=customer_id)
            except Customer.DoesNotExist:
                return Response({'error': 'Customer not found'}, status=status.HTTP_404_NOT_FOUND)

        if not customer:
            return Response({'error': 'No customer associated with sale'}, status=status.HTTP_400_BAD_REQUEST)

        # Create invoice
        invoice = Invoice.objects.create(
            sale=sale,
            customer=customer,
            due_date=due_date or (timezone.now().date() + timezone.timedelta(days=30)),
            subtotal=sale.total_amount,
            total_amount=sale.final_amount,
            status='sent',
            notes=f'Invoice generated from sale {sale.receipt_number}'
        )

        # Create invoice items from sale items
        for sale_item in sale.saleitem_set.all():
            InvoiceItem.objects.create(
                invoice=invoice,
                product=sale_item.product,
                description=sale_item.product.name,
                quantity=sale_item.quantity,
                unit_price=sale_item.unit_price,
                tax_rate=0,  # Could be enhanced to include tax
                discount_amount=sale_item.discount
            )

        serializer = self.get_serializer(invoice)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

class InvoiceItemViewSet(viewsets.ModelViewSet):
    queryset = InvoiceItem.objects.all()
    serializer_class = InvoiceItemSerializer
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['invoice', 'product']
    search_fields = ['description', 'product__name']
    ordering_fields = ['quantity', 'unit_price']
    ordering = ['description']


class AuditLogViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only viewset for audit logs (admin/manager only)"""
    queryset = AuditLog.objects.select_related('user__user')
    serializer_class = AuditLogSerializer
    pagination_class = StandardResultsPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['operation', 'user', 'entity_type']
    search_fields = ['description', 'user__user__username']
    ordering_fields = ['timestamp', 'operation']
    ordering = ['-timestamp']

    def get_queryset(self):
        """Filter audit logs based on user permissions"""
        queryset = super().get_queryset()

        # Only admin and manager can view audit logs
        if not hasattr(self.request.user, 'userprofile'):
            return queryset.none()

        user_profile = self.request.user.userprofile
        if user_profile.role not in ['admin', 'manager']:
            return queryset.none()

        # Managers can only see logs from their branch
        if user_profile.role == 'manager' and user_profile.branch:
            queryset = queryset.filter(
                Q(user__branch=user_profile.branch) |
                Q(user__isnull=True)  # System operations
            )

        return queryset
