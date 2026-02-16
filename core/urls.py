# core/urls.py
from django.urls import path
from . import views
from .permissions import staff_blocked, staff_allowed
from .views import account_ledger_api, account_balance_api, account_ledger_view
from .views import party_ledger_api, party_balance_api
from .views import trial_balance_api, party_ledger_view
from django.views.generic import RedirectView
from django.views.generic import TemplateView
from django.contrib.auth import views as auth_views
from .views import run_backup_job
from .views_public import robots_txt
from .views_public import sitemap_xml
from .views_public import google_verify





urlpatterns = [
    # =========================
    # Public pages
    # =========================
    path("", views.landing_page, name="landing"),
    path("signup/", views.signup_page, name="signup"),
    path("signup/submit/", views.signup_submit, name="signup_submit"),

    # =========================
    # Auth
    # =========================
    path("login/", auth_views.LoginView.as_view(template_name="registration/login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),

    # =========================
    # App (authenticated)
    # =========================
    path("dashboard/", views.dashboard, name="dashboard"),

    # Super Admin (global)
    path("superadmin/", views.superadmin_dashboard, name="superadmin_dashboard"),
    path("superadmin/owners/<int:owner_id>/", views.superadmin_owner_detail, name="superadmin_owner_detail"),
    path("superadmin/owners/<int:owner_id>/toggle-suspend/", views.superadmin_toggle_suspend, name="superadmin_toggle_suspend"),
    path("superadmin/owners/<int:owner_id>/subscription/", views.superadmin_subscription_update, name="superadmin_subscription_update"),
    path("superadmin/owners/<int:owner_id>/purge/", views.superadmin_hard_purge_owner, name="superadmin_hard_purge_owner"),
    # =========================
    # Customers
    # =========================
    path("parties/customers/", staff_blocked(views.customer_list), name="customer_list"),
    path("parties/customers/new/", staff_allowed(views.customer_create), name="customer_create"),
    path("parties/customers/<int:pk>/edit/", staff_blocked(views.customer_edit), name="customer_edit"),

    # =========================
    # Suppliers
    # =========================
    path("parties/suppliers/", staff_blocked(views.supplier_list), name="supplier_list"),
    path("parties/suppliers/new/", staff_allowed(views.supplier_create), name="supplier_create"),
    path("parties/suppliers/<int:pk>/edit/", staff_blocked(views.supplier_edit), name="supplier_edit"),

    # =========================
    # Products
    # =========================
    path("products/", staff_blocked(views.product_list), name="product_list"),
    path("products/new/", staff_allowed(views.product_create), name="product_create"),
    path("products/<int:pk>/edit/", staff_blocked(views.product_edit), name="product_edit"),

    # =========================
    # Sales
    # =========================
    path("sales/", staff_blocked(views.sales_list), name="sales_list"),
    path("sales/new/", staff_allowed(views.sales_new), name="sales_new"),
    path("sales/<int:pk>/post/", staff_allowed(views.sales_post), name="sales_post"),
    path("sales/<int:pk>/edit/", staff_blocked(views.sales_edit), name="sales_edit"),
    path("sales/<int:pk>/delete/", staff_blocked(views.sales_delete), name="sales_delete"),

    # =========================
    # Purchases
    # =========================
    path("purchases/", staff_blocked(views.purchase_list), name="purchase_list"),
    path("purchases/new/", staff_allowed(views.purchase_new), name="purchase_new"),
    path("purchases/<int:pk>/post/", staff_allowed(views.purchase_post), name="purchase_post"),
    path("purchases/<int:pk>/edit/", staff_blocked(views.purchase_edit), name="purchase_edit"),
    path("purchases/<int:pk>/delete/", staff_blocked(views.purchase_delete), name="purchase_delete"),

    # =========================
    # Returns (staff allowed)
    # =========================
    path("returns/sales/new/", staff_allowed(views.sales_return_new), name="sales_return_new"),
    path("returns/purchase/new/", staff_allowed(views.purchase_return_new), name="purchase_return_new"),

    # =========================
    # Payments / Receipts
    # =========================
    path("payments/", staff_blocked(views.payment_list), name="payment_list"),
    path("payments-list/", RedirectView.as_view(pattern_name="payment_list", permanent=False), name="payments_list"),
    path("payments/new/", staff_allowed(views.payment_new), name="payment_new"),

    # =========================
    # Adjustments (staff allowed)
    # =========================
    path("adjustments/", staff_allowed(views.adjustments_page), name="adjustments_page"),

    # =========================
    # Reports & utilities (STAFF blocked)
    # =========================
    path("day-summary/", staff_blocked(views.day_summary), name="day_summary"),
    path("stock-report/", staff_blocked(views.stock_report), name="stock_report"),
    path("customer-balances/", staff_blocked(views.customer_balances), name="customer_balances"),
    path("supplier-balances/", staff_blocked(views.supplier_balances), name="supplier_balances"),
    path("reports/party/<int:pk>/", staff_blocked(views.party_statement), name="party_statement"),
    path("reports/ledger/", staff_blocked(views.account_ledger), name="account_ledger"),
    path("reports/trial-balance/", staff_blocked(views.trial_balance), name="trial_balance"),
    path("profit-loss/", staff_blocked(views.profit_loss), name="profit_loss"),
    path("balance-sheet/", staff_blocked(views.balance_sheet), name="balance_sheet"),
    path("product-ledger/", staff_blocked(views.product_ledger), name="product_ledger"),
    path("ledger/customers/", staff_blocked(views.customer_ledger), name="customer_ledger"),
    path("ledger/suppliers/", staff_blocked(views.supplier_ledger), name="supplier_ledger"),

    # =========================
    # User cash/bank accounts (STAFF blocked)
    # =========================
    path("user-accounts/", staff_blocked(views.user_accounts), name="user_accounts"),
    path("user-accounts/<int:pk>/delete/", staff_blocked(views.user_account_delete), name="user_account_delete"),

    # =========================
    # Backups (STAFF blocked)
    # =========================
    path("backup/", staff_blocked(views.backup_dashboard), name="backup_dashboard"),
    path("backup/create/", staff_blocked(views.create_backup), name="create_backup"),
    path("backup/restore/", staff_blocked(views.restore_backup), name="restore_backup"),
    path("backup/download/<str:filename>/", staff_blocked(views.download_backup), name="download_backup"),

    # =========================
    # Subscription / Profile (STAFF blocked)
    # =========================
    path("subscription/", staff_blocked(views.subscription_page), name="subscription_page"),
    path("profile/", staff_blocked(views.owner_profile_page), name="owner_profile_page"),
    path("company/", staff_blocked(views.company_page), name="company_page"),

    # =========================
    # Misc
    # =========================
    path("tenant-check/", views.tenant_check, name="tenant_check"),

    # =========================
    # Ledger APIs (STAFF blocked)
    # =========================
    path("ledger/account-balance/", staff_blocked(account_balance_api), name="account_balance_api"),
    path("ledger/account-ledger/", staff_blocked(account_ledger_api), name="account_ledger_api"),
    path("ledger/party-balance/", staff_blocked(party_balance_api), name="party_balance_api"),
    path("ledger/party-ledger/", staff_blocked(party_ledger_api), name="party_ledger_api"),
    path("ledger/trial-balance/", staff_blocked(trial_balance_api), name="trial_balance_api"),
    path("ledger/account-ledger/view/", staff_blocked(account_ledger_view), name="account_ledger_view"),
    path("ledger/party/<int:party_id>/", staff_blocked(party_ledger_view), name="party_ledger"),

    path("offline/", views.offline_page, name="offline_page"),
    path("service-worker.js", views.service_worker, name="service_worker"),

    path("privacy/", views.privacy_policy, name="privacy_policy"),
    path("terms/", views.terms_conditions, name="terms_conditions"),
    path("refund/", views.refund_policy, name="refund_policy"),
    path("service/", views.service_policy, name="service_policy"),

    path("password-reset/", auth_views.PasswordResetView.as_view(), name="password_reset"),
    path("password_reset/", auth_views.PasswordResetView.as_view(), name="password_reset_legacy"),

    path("password-reset/done/", auth_views.PasswordResetDoneView.as_view(), name="password_reset_done"),
    path("reset/<uidb64>/<token>/", auth_views.PasswordResetConfirmView.as_view(), name="password_reset_confirm"),
    path("reset/done/", auth_views.PasswordResetCompleteView.as_view(), name="password_reset_complete"),
    
    # =========================
    # Tax Pack (STAFF blocked)
    # =========================
    path("tax-pack/", staff_blocked(views.tax_pack_page), name="tax_pack_page"),
    path("tax-pack/sales-ledger/", staff_blocked(views.tax_sales_ledger_download), name="tax_sales_ledger"),
    path("tax-pack/purchase-ledger/", staff_blocked(views.tax_purchase_ledger_download), name="tax_purchase_ledger"),
    path("tax-pack/payments-ledger/", staff_blocked(views.tax_payments_ledger_download), name="tax_payments_ledger"),
    path("tax-pack/products/", staff_blocked(views.tax_products_download), name="tax_products_download"),
    path("tax-pack/parties/", staff_blocked(views.tax_parties_download), name="tax_parties_download"),
    path("tax-pack/accounts/", staff_blocked(views.tax_accounts_download), name="tax_accounts_download"),
    path("tax-pack/full-zip/", staff_blocked(views.tax_pack_zip_download), name="tax_pack_zip_download"),
]
urlpatterns += [
    path("internal/run-backup/", run_backup_job, name="run_backup_job"),
    path("robots.txt", robots_txt, name="robots_txt"),
    path("sitemap.xml", sitemap_xml, name="sitemap_xml"),
    path("googlea8d36177338cf4b5.html", google_verify),


]
