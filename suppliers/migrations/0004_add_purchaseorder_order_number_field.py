# Generated manually to add missing order_number field to PurchaseOrder

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('suppliers', '0003_remove_purchaseorder_expected_delivery_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='purchaseorder',
            name='order_number',
            field=models.CharField(blank=True, max_length=50, unique=True),
        ),
    ]