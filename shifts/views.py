from rest_framework import viewsets, generics, status, serializers
from rest_framework.response import Response
from myshop.pagination import StandardResultsPagination
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.filters import SearchFilter, OrderingFilter
from django.db.models import Prefetch
import logging
from decimal import Decimal, InvalidOperation
from django.db import transaction, IntegrityError
from rest_framework.permissions import IsAuthenticated
logger = logging.getLogger(__name__)
from .models import Shift
from .serializers import ShiftSerializer, ShiftListSerializer, shift_list_queryset
from sales.models import Sale

class ShiftViewSet(viewsets.ModelViewSet):
    queryset = Shift.objects.all()
    serializer_class = ShiftListSerializer
    pagination_class = StandardResultsPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['cashier', 'status', 'start_time', 'end_time']
    search_fields = ['cashier__user__username', 'cashier__user__first_name', 'cashier__user__last_name']
    ordering_fields = ['start_time', 'end_time', 'total_sales', 'transaction_count']
    ordering = ['-start_time']
    
    def list(self, request, *args, **kwargs):
        # Check if limit is provided and bypass pagination
        limit = request.query_params.get('limit')
        if limit:
            try:
                queryset = self.filter_queryset(self.get_queryset())
                # Apply limit directly
                page = queryset[:int(limit)]
                serializer = self.get_serializer(page, many=True)
                return Response(serializer.data)
            except ValueError:
                pass
        # Default behavior
        return super().list(request, *args, **kwargs)
    
    def get_queryset(self):
        queryset = shift_list_queryset(super().get_queryset())
        if not _is_manager(self.request.user):
            queryset = queryset.filter(cashier=_profile(self.request.user))
        
        # Filter by user_id if provided - ensure data isolation between users
        user_id = self.request.query_params.get('user_id')
        if user_id:
            queryset = queryset.filter(cashier__user_id=user_id)
        
        # Filter by date range if provided
        start_date = self.request.query_params.get('start_date')
        end_date = self.request.query_params.get('end_date')
        
        if start_date:
            queryset = queryset.filter(start_time__date__gte=start_date)
        if end_date:
            queryset = queryset.filter(start_time__date__lte=end_date)
            
        return queryset


def _profile(user):
    return getattr(user, 'userprofile', None)


def _is_manager(user):
    from users.permissions import has_manager_rights
    return has_manager_rights(user)


def _target_profile(request):
    """The profile a request may act on: managers may pass user_id, others get their own."""
    from users.models import UserProfile
    own = _profile(request.user)
    user_id = request.query_params.get('user_id') or request.data.get('user_id')
    if not user_id or not _is_manager(request.user):
        return own
    return (UserProfile.objects.filter(id=user_id).first()
            or UserProfile.objects.filter(user_id=user_id).first())


def _last_closed_info(profile):
    last = Shift.objects.filter(cashier=profile, status='closed').order_by('-end_time').first() if profile else None
    if not last:
        return None
    return {
        'id': last.id, 'end_time': last.end_time,
        'closing_balance': float(last.closing_balance or 0),
        'total_sales': float(last.total_sales or 0),
        'discrepancy': float(last.discrepancy or 0), 'status': last.status,
    }


class CurrentShiftView(generics.RetrieveAPIView):
    """Current open shift of the logged-in cashier (managers may pass user_id)."""
    serializer_class = ShiftSerializer
    permission_classes = [IsAuthenticated]

    def retrieve(self, request, *args, **kwargs):
        profile = _target_profile(request)
        shift = Shift.objects.filter(cashier=profile, status='open').prefetch_related(
            Prefetch('sale_set', queryset=Sale.objects.select_related(
                'customer', 'voided_by__user', 'edited_by__user').prefetch_related(
                'saleitem_set__product', 'payment_set'))
        ).select_related('cashier__user', 'approved_by__user').first() if profile else None
        if not shift:
            return Response({'has_active_shift': False, 'status': 'closed',
                             'last_shift_info': _last_closed_info(profile)})
        return Response(self.get_serializer(shift).data)


class StartShiftView(generics.CreateAPIView):
    """Start a new shift for the logged-in user."""
    serializer_class = ShiftSerializer
    permission_classes = [IsAuthenticated]

    def create(self, request, *args, **kwargs):
        from users.models import UserProfile
        profile = _profile(request.user) or UserProfile.objects.create(
            user=request.user, role='cashier', is_active=True)
        raw = request.data.get('opening_balance', request.data.get('starting_cash', 0)) or 0
        try:
            opening_balance = Decimal(str(raw))
        except (InvalidOperation, ValueError):
            return Response({'error': 'Invalid opening balance'}, status=status.HTTP_400_BAD_REQUEST)
        if opening_balance < 0:
            return Response({'error': 'Opening balance cannot be negative'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            with transaction.atomic():
                shift = Shift.objects.create(cashier=profile, opening_balance=opening_balance,
                                             start_time=timezone.now(), status='open')
        except IntegrityError:  # one_open_shift_per_cashier
            existing = Shift.objects.filter(cashier=profile, status='open').first()
            return Response({'error': 'You already have an active shift',
                             'shift_id': existing.id if existing else None},
                            status=status.HTTP_400_BAD_REQUEST)
        return Response(ShiftSerializer(shift).data, status=status.HTTP_201_CREATED)


class EndShiftView(generics.GenericAPIView):
    """Close a shift and reconcile the cash drawer.

    Cashiers close their own open shift; managers may close any (shift_id).
    Expected cash = opening balance + cash payments - cash refunds, all derived
    from Payment/Return records.
    """
    serializer_class = ShiftSerializer
    permission_classes = [IsAuthenticated]

    def post(self, request, *args, **kwargs):
        actual = request.data.get('ending_cash')
        if actual is None:
            actual = request.data.get('actual_cash')
        if actual is None or actual == '':
            return Response({'error': 'ending_cash (counted cash) is required'},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            actual = Decimal(str(actual))
        except (InvalidOperation, ValueError):
            return Response({'error': 'Invalid cash amount'}, status=status.HTTP_400_BAD_REQUEST)

        own = _profile(request.user)
        with transaction.atomic():
            shift_id = request.data.get('shift_id') or request.query_params.get('shift_id')
            qs = Shift.objects.select_for_update()
            if shift_id:
                shift = qs.filter(id=shift_id).first()
                if shift and (own is None or shift.cashier_id != own.id) and not _is_manager(request.user):
                    return Response({'error': 'You can only close your own shift'},
                                    status=status.HTTP_403_FORBIDDEN)
            else:
                profile = _target_profile(request)
                shift = qs.filter(cashier=profile, status='open').first() if profile else None
            if not shift:
                return Response({'error': 'No active shift found. Please start a shift first.',
                                 'no_active_shift': True}, status=status.HTTP_404_NOT_FOUND)
            if shift.status == 'closed':
                return Response({'message': 'Shift is already closed', 'shift': ShiftSerializer(shift).data,
                                 'shift_status': 'closed'}, status=status.HTTP_200_OK)

            expected = shift.expected_cash()  # also refreshes totals
            discrepancy = actual - expected
            shift.end_time = timezone.now()
            shift.closing_balance = actual
            shift.discrepancy = discrepancy
            shift.status = 'closed'
            shift.save()

        if discrepancy < 0:
            dtype, desc = 'shortage', f'Shortage: KSh {abs(discrepancy):.2f}'
        elif discrepancy > 0:
            dtype, desc = 'overage', f'Overage: KSh {abs(discrepancy):.2f}'
        else:
            dtype, desc = 'balanced', 'Perfect balance'
        reconciliation = {
            'shift_id': shift.id, 'shift_status': shift.status,
            'opening_balance': float(shift.opening_balance),
            'cash_sales': float(shift.cash_sales), 'card_sales': float(shift.card_sales),
            'mobile_sales': float(shift.mobile_sales), 'total_sales': float(shift.total_sales),
            'total_returns': float(shift.total_returns),
            'cash_refunds': float(shift._cash_refunds),
            'expected_closing_balance': float(expected),
            'actual_closing_balance': float(actual),
            'discrepancy': float(discrepancy), 'discrepancy_type': dtype,
            'discrepancy_description': desc,
            'end_time': shift.end_time.isoformat() if shift.end_time else None,
        }
        return Response({'message': 'Shift ended successfully', 'shift': ShiftSerializer(shift).data,
                         'reconciliation': reconciliation, 'shift_status': 'closed'},
                        status=status.HTTP_200_OK)

    def put(self, request, *args, **kwargs):
        return self.post(request, *args, **kwargs)

    def patch(self, request, *args, **kwargs):
        return self.post(request, *args, **kwargs)


class AllShiftsView(generics.ListAPIView):
    """Shifts list. Managers see all (filterable); cashiers see only their own."""
    serializer_class = ShiftListSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = StandardResultsPagination

    def get_queryset(self):
        queryset = shift_list_queryset(Shift.objects.all()).order_by('-start_time')
        params = self.request.query_params
        if not _is_manager(self.request.user):
            queryset = queryset.filter(cashier=_profile(self.request.user))
        else:
            if params.get('user_id'):
                queryset = queryset.filter(cashier__user_id=params['user_id'])
            if params.get('cashier_id'):
                queryset = queryset.filter(cashier_id=params['cashier_id'])
        if params.get('status'):
            queryset = queryset.filter(status=params['status'])
        if params.get('start_date'):
            queryset = queryset.filter(start_time__date__gte=params['start_date'])
        if params.get('end_date'):
            queryset = queryset.filter(start_time__date__lte=params['end_date'])
        return queryset


class AdminShiftManagementView(generics.GenericAPIView):
    """Manager/admin: reopen or force-close a shift (POST/PUT/PATCH)."""
    serializer_class = ShiftSerializer
    permission_classes = [IsAuthenticated]
    lookup_field = 'id'
    lookup_url_kwarg = 'shift_id'

    def get_queryset(self):
        return Shift.objects.all()

    def post(self, request, *args, **kwargs):
        if not _is_manager(request.user):
            return Response({'error': 'Admin or Manager role required'}, status=status.HTTP_403_FORBIDDEN)
        action = kwargs.get('action')
        shift = self.get_object()
        if action == 'reopen':
            if Shift.objects.filter(cashier=shift.cashier, status='open').exclude(pk=shift.pk).exists():
                return Response({'error': 'This cashier already has an open shift'},
                                status=status.HTTP_400_BAD_REQUEST)
            shift.end_time = None
            shift.status = 'open'
            shift.closing_balance = None
            shift.discrepancy = None
            shift.save()
        elif action == 'force_close':
            shift.recompute_totals()
            shift.end_time = timezone.now()
            shift.status = 'closed'
            shift.save()
        else:
            return Response({'error': 'Unknown action'}, status=status.HTTP_400_BAD_REQUEST)
        return Response(ShiftSerializer(shift).data)

    put = post
    patch = post
