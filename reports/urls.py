from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    ReportViewSet, SalesSummaryView, InventorySummaryView,
    CustomerSummaryView, ShiftSummaryView
)

router = DefaultRouter()
router.register(r'reports', ReportViewSet)

urlpatterns = [
    path('', include(router.urls)),
    path('sales-summary/', SalesSummaryView.as_view(), name='sales-summary'),
    path('inventory-summary/', InventorySummaryView.as_view(), name='inventory-summary'),
    path('customer-summary/', CustomerSummaryView.as_view(), name='customer-summary'),
    path('shift-summary/', ShiftSummaryView.as_view(), name='shift-summary'),
]