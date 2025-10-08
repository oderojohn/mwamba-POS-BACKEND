from rest_framework import serializers
from .models import Supplier, SupplierPriceHistory, PurchaseOrder, PurchaseOrderItem

class SupplierSerializer(serializers.ModelSerializer):
    class Meta:
        model = Supplier
        fields = '__all__'

class SupplierPriceHistorySerializer(serializers.ModelSerializer):
    supplier_name = serializers.CharField(source='supplier.name', read_only=True)
    product_name = serializers.CharField(source='product.name', read_only=True)
    
    class Meta:
        model = SupplierPriceHistory
        fields = '__all__'

class PurchaseOrderItemSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source='product.name', read_only=True)
    total_price = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    is_fully_received = serializers.BooleanField(read_only=True)

    class Meta:
        model = PurchaseOrderItem
        fields = '__all__'

class PurchaseOrderSerializer(serializers.ModelSerializer):
    supplier_name = serializers.CharField(source='supplier.name', read_only=True)
    items = PurchaseOrderItemSerializer(many=True, read_only=True)

    class Meta:
        model = PurchaseOrder
        fields = '__all__'

    def create(self, validated_data):
        items_data = self.context['request'].data.get('items', [])
        purchase_order = super().create(validated_data)

        # Create order items
        for item_data in items_data:
            PurchaseOrderItem.objects.create(
                purchase_order=purchase_order,
                product_id=item_data['product'],
                quantity=item_data['quantity'],
                unit_price=item_data['unit_price'],
                batch_number=item_data.get('batch_number', ''),
                expiry_date=item_data.get('expiry_date')
            )

        # Update total amount
        purchase_order.total_amount = sum(item.total_price for item in purchase_order.items.all())
        purchase_order.save()

        return purchase_order