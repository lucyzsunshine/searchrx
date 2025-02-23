from django.urls import path

from . import views


app_name = "search"

urlpatterns = [
    path("", views.index, name="index"),
    path("results", views.list_search_results, name="list_search_results"),
    path("es_search", views.es_search, name="es_search"),
]
