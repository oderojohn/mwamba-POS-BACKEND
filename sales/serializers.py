from rest_framework import serializers
from .models import Cart, CartItem, Sale, SaleItem, Return, Invoice, InvoiceItem

class CartItemSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source='product.name', read_only=True)

    class Meta:
        model = CartItem
        fields = '__all__'
        read_only_fields = ('id',)

class CartSerializer(serializers.ModelSerializer):
    items = CartItemSerializer(source='cartitem_set', many=True, read_only=True)
    customer_name = serializers.CharField(source='customer.name', read_only=True)
    cashier_name = serializers.CharField(source='cashier.user.username', read_only=True)

    class Meta:
        model = Cart
        fields = '__all__'
        read_only_fields = ('id', 'created_at', 'updated_at')

class SaleItemSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source='product.name', read_only=True)

    class Meta:
        model = SaleItem
        fields = '__all__'

class SaleSerializer(serializers.ModelSerializer):
    items = SaleItemSerializer(many=True, read_only=True)
    customer_name = serializers.SerializerMethodField()
    payment_method = serializers.SerializerMethodField()
    split_data = serializers.SerializerMethodField()
    voided_by_name = serializers.SerializerMethodField()

    class Meta:
        model = Sale
        fields = '__all__'

    def get_customer_name(self, obj):
        return obj.customer.name if obj.customer else None

    def get_payment_method(self, obj):
        # Get payment method from related payment record
        payment = obj.payment_set.first()
        if payment:
            return payment.payment_type
        else:
            # Fallback: return 'cash' to prevent N/A display
            # This should not happen due to validation, but provides safety net
            return 'cash'

    def get_split_data(self, obj):
        # Get split payment data from related payment record
        payment = obj.payment_set.filter(payment_type='split').first()
        return payment.split_data if payment and payment.split_data else None

    def get_voided_by_name(self, obj):
        return obj.voided_by.user.username if obj.voided_by else None

class ReturnSerializer(serializers.ModelSerializer):
    sale_receipt = serializers.CharField(source='sale.receipt_number', read_only=True)
    processed_by_name = serializers.CharField(source='processed_by.user.username', read_only=True)

    class Meta:
        model = Return
        fields = '__all__'

class InvoiceItemSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source='product.name', read_only=True)
    subtotal = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    tax_amount = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    total = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)

    class Meta:
        model = InvoiceItem
        fields = '__all__'

class InvoiceSerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source='customer.name', read_only=True)
    items = InvoiceItemSerializer(many=True, read_only=True)
    is_overdue = serializers.BooleanField(read_only=True)
    created_by_name = serializers.CharField(source='created_by.user.username', read_only=True)

    class Meta:
        model = Invoice
        fields = '__all__'
        read_only_fields = ('invoice_number', 'created_at', 'updated_at')

    def create(self, validated_data):
        items_data = self.context['request'].data.get('items', [])
        invoice = super().create(validated_data)

        # Create invoice items
        for item_data in items_data:
            InvoiceItem.objects.create(
                invoice=invoice,
                product_id=item_data.get('product'),
                description=item_data['description'],
                quantity=item_data['quantity'],
                unit_price=item_data['unit_price'],
                tax_rate=item_data.get('tax_rate', 0),
                discount_amount=item_data.get('discount_amount', 0)
            )

        # Calculate totals
        invoice.subtotal = sum(item.subtotal for item in invoice.items.all())
        invoice.tax_amount = sum(item.tax_amount for item in invoice.items.all())
        invoice.discount_amount = sum(item.discount_amount for item in invoice.items.all())
        invoice.total_amount = invoice.subtotal + invoice.tax_amount - invoice.discount_amount
        invoice.save()

        return invoice