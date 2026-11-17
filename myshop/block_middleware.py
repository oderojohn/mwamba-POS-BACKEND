"""Kill switch: with API_BLOCKED=1 in the environment, every request gets a 503.

Placed after CorsMiddleware so the browser can still read the JSON message.
Remove the variable (and redeploy) to let requests through again.
"""
import os

from django.http import JsonResponse


class BlockAllRequestsMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        self.blocked = os.environ.get('API_BLOCKED', '').strip().lower() in ('1', 'true', 'on', 'yes')

    def __call__(self, request):
        if self.blocked:
            return JsonResponse(
                {'detail': 'The system is temporarily unavailable. Please try again later.'},
                status=503,
            )
        return self.get_response(request)
