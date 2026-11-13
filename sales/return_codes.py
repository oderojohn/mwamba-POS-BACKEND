"""Return code lookup. Redemption happens inside checkout (sales/services/checkout.py)."""
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import ReturnCode


@api_view(['POST'])
def validate_return_code(request):
    code = str(request.data.get('code') or '').strip().upper()
    if not code:
        return Response({'valid': False, 'error': 'Return code is required'}, status=status.HTTP_400_BAD_REQUEST)
    rc = ReturnCode.objects.select_related('return_record').filter(code=code).first()
    if not rc:
        return Response({'valid': False, 'error': 'Return code not found'}, status=status.HTTP_404_NOT_FOUND)
    if rc.status != 'active':
        label = 'already been used' if rc.status == 'used' else rc.status
        return Response({'valid': False, 'error': f'Return code has {label}' if rc.status == 'used'
                         else f'Return code is {label}', 'status': rc.status},
                        status=status.HTTP_400_BAD_REQUEST)
    return Response({
        'valid': True,
        'code': rc.code,
        'refund_amount': str(rc.refund_amount),
        'original_receipt_number': rc.original_receipt_number,
        'status': rc.status,
        'return_receipt': rc.return_record.receipt_number if rc.return_record else None,
    })
