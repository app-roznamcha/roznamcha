from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, Iterable, List, Optional

from core.models import (
    Product,
    PurchaseInvoice,
    PurchaseReturnItem,
    SalesInvoiceItem,
    SalesReturnItem,
    StockAdjustment,
)


MONEY_ZERO = Decimal("0.00")
QTY_ZERO = Decimal("0.000")

CONFIDENCE_HIGH = "High Confidence"
CONFIDENCE_ESTIMATED = "Estimated"
CONFIDENCE_INCOMPLETE = "Incomplete"


@dataclass
class ValuationMovement:
    product_id: int
    product_code: str
    product_name: str
    movement_date: object
    created_at: object
    movement_type: str
    source_model: str
    source_id: int
    reference: str
    qty_in: Decimal = QTY_ZERO
    qty_out: Decimal = QTY_ZERO
    revenue_value: Decimal = MONEY_ZERO
    cost_value_input: Decimal = MONEY_ZERO
    cost_rate_input: Optional[Decimal] = None
    allocated_header_charge: Decimal = MONEY_ZERO


@dataclass
class ProductValuationState:
    product: Product
    running_qty: Decimal = QTY_ZERO
    running_inventory_value: Decimal = MONEY_ZERO
    opening_qty: Decimal = QTY_ZERO
    opening_value: Decimal = MONEY_ZERO
    qty_sold: Decimal = QTY_ZERO
    gross_sales_revenue: Decimal = MONEY_ZERO
    sales_return_revenue: Decimal = MONEY_ZERO
    sales_revenue: Decimal = MONEY_ZERO
    cogs: Decimal = MONEY_ZERO
    gross_profit: Decimal = MONEY_ZERO
    confidence_label: str = CONFIDENCE_HIGH
    flags: set[str] = field(default_factory=set)

    def average_cost(self) -> Optional[Decimal]:
        if self.running_qty > 0:
            return self.running_inventory_value / self.running_qty
        return None

    def mark_estimated(self, reason: str) -> None:
        if self.confidence_label != CONFIDENCE_INCOMPLETE:
            self.confidence_label = CONFIDENCE_ESTIMATED
        self.flags.add(reason)

    def mark_incomplete(self, reason: str) -> None:
        self.confidence_label = CONFIDENCE_INCOMPLETE
        self.flags.add(reason)


def _line_total(quantity_units: Decimal, unit_price: Decimal, discount_amount: Decimal) -> Decimal:
    qty = quantity_units or QTY_ZERO
    price = unit_price or MONEY_ZERO
    discount = discount_amount or MONEY_ZERO
    return (qty * price) - discount


def _normalize_movement_order(movement: ValuationMovement):
    movement_rank = {
        "PURCHASE": 10,
        "STOCK_ADJ_UP": 20,
        "PURCHASE_RETURN": 30,
        "STOCK_ADJ_DOWN": 40,
        "SALE": 50,
        "SALES_RETURN": 60,
    }
    return (
        movement.movement_date,
        movement.created_at,
        movement_rank.get(movement.movement_type, 999),
        movement.source_id,
    )


def _allocate_purchase_header_charges(invoice: PurchaseInvoice):
    lines = list(invoice.items.all())
    if not lines:
        return []

    header_charge_total = (invoice.freight_charges or MONEY_ZERO) + (invoice.other_charges or MONEY_ZERO)
    line_values = [_line_total(item.quantity_units, item.unit_price, item.discount_amount) for item in lines]
    total_line_value = sum(line_values, MONEY_ZERO)
    allocations: Dict[int, Decimal] = {}
    allocation_issue = False

    if header_charge_total != MONEY_ZERO and total_line_value <= MONEY_ZERO:
        allocation_issue = True
        allocations = {item.id: MONEY_ZERO for item in lines}
    else:
        allocated_so_far = MONEY_ZERO
        for index, item in enumerate(lines):
            if header_charge_total == MONEY_ZERO:
                allocated = MONEY_ZERO
            elif index == len(lines) - 1:
                allocated = header_charge_total - allocated_so_far
            else:
                allocated = header_charge_total * (line_values[index] / total_line_value)
                allocated_so_far += allocated
            allocations[item.id] = allocated

    return [(item, line_values[index], allocations[item.id], allocation_issue) for index, item in enumerate(lines)]


def _build_product_movements(owner, date_to) -> Dict[int, List[ValuationMovement]]:
    movement_map: Dict[int, List[ValuationMovement]] = defaultdict(list)

    purchase_invoices = (
        PurchaseInvoice.objects.filter(owner=owner, posted=True, invoice_date__lte=date_to)
        .prefetch_related("items__product")
        .order_by("invoice_date", "created_at", "id")
    )
    for invoice in purchase_invoices:
        for item, line_value, allocated_charge, allocation_issue in _allocate_purchase_header_charges(invoice):
            movement = ValuationMovement(
                product_id=item.product_id,
                product_code=item.product.code,
                product_name=item.product.name,
                movement_date=invoice.invoice_date,
                created_at=item.created_at,
                movement_type="PURCHASE",
                source_model="PurchaseInvoice",
                source_id=invoice.id,
                reference=f"PI #{invoice.id}",
                qty_in=item.quantity_units or QTY_ZERO,
                revenue_value=MONEY_ZERO,
                cost_value_input=line_value + allocated_charge,
                cost_rate_input=((line_value + allocated_charge) / item.quantity_units) if item.quantity_units else None,
                allocated_header_charge=allocated_charge,
            )
            if allocation_issue:
                movement.reference = f"{movement.reference} (charge allocation unresolved)"
            movement_map[item.product_id].append(movement)

    purchase_returns = (
        PurchaseReturnItem.objects.filter(
            owner=owner,
            purchase_return__owner=owner,
            purchase_return__posted=True,
            purchase_return__return_date__lte=date_to,
        )
        .select_related("purchase_return", "product")
        .order_by("purchase_return__return_date", "created_at", "id")
    )
    for item in purchase_returns:
        line_value = _line_total(item.quantity_units, item.unit_price, item.discount_amount)
        movement_map[item.product_id].append(
            ValuationMovement(
                product_id=item.product_id,
                product_code=item.product.code,
                product_name=item.product.name,
                movement_date=item.purchase_return.return_date,
                created_at=item.created_at,
                movement_type="PURCHASE_RETURN",
                source_model="PurchaseReturn",
                source_id=item.purchase_return_id,
                reference=f"PR #{item.purchase_return_id}",
                qty_out=item.quantity_units or QTY_ZERO,
                cost_value_input=line_value,
                cost_rate_input=(line_value / item.quantity_units) if item.quantity_units else None,
            )
        )

    sales = (
        SalesInvoiceItem.objects.filter(
            owner=owner,
            sales_invoice__owner=owner,
            sales_invoice__posted=True,
            sales_invoice__invoice_date__lte=date_to,
        )
        .select_related("sales_invoice", "product")
        .order_by("sales_invoice__invoice_date", "created_at", "id")
    )
    for item in sales:
        revenue_value = _line_total(item.quantity_units, item.unit_price, item.discount_amount)
        movement_map[item.product_id].append(
            ValuationMovement(
                product_id=item.product_id,
                product_code=item.product.code,
                product_name=item.product.name,
                movement_date=item.sales_invoice.invoice_date,
                created_at=item.created_at,
                movement_type="SALE",
                source_model="SalesInvoice",
                source_id=item.sales_invoice_id,
                reference=f"SI #{item.sales_invoice_id}",
                qty_out=item.quantity_units or QTY_ZERO,
                revenue_value=revenue_value,
            )
        )

    sales_returns = (
        SalesReturnItem.objects.filter(
            owner=owner,
            sales_return__owner=owner,
            sales_return__posted=True,
            sales_return__return_date__lte=date_to,
        )
        .select_related("sales_return", "product")
        .order_by("sales_return__return_date", "created_at", "id")
    )
    for item in sales_returns:
        revenue_value = _line_total(item.quantity_units, item.unit_price, item.discount_amount)
        movement_map[item.product_id].append(
            ValuationMovement(
                product_id=item.product_id,
                product_code=item.product.code,
                product_name=item.product.name,
                movement_date=item.sales_return.return_date,
                created_at=item.created_at,
                movement_type="SALES_RETURN",
                source_model="SalesReturn",
                source_id=item.sales_return_id,
                reference=f"SR #{item.sales_return_id}",
                qty_in=item.quantity_units or QTY_ZERO,
                revenue_value=-revenue_value,
            )
        )

    adjustments = (
        StockAdjustment.objects.filter(owner=owner, posted=True, date__lte=date_to)
        .select_related("product")
        .order_by("date", "created_at", "id")
    )
    for adjustment in adjustments:
        qty = adjustment.qty or QTY_ZERO
        value = qty * (adjustment.unit_cost or MONEY_ZERO)
        movement_map[adjustment.product_id].append(
            ValuationMovement(
                product_id=adjustment.product_id,
                product_code=adjustment.product.code,
                product_name=adjustment.product.name,
                movement_date=adjustment.date,
                created_at=adjustment.created_at,
                movement_type="STOCK_ADJ_UP" if adjustment.direction == "UP" else "STOCK_ADJ_DOWN",
                source_model="StockAdjustment",
                source_id=adjustment.id,
                reference=f"ADJ #{adjustment.id}",
                qty_in=qty if adjustment.direction == "UP" else QTY_ZERO,
                qty_out=qty if adjustment.direction == "DOWN" else QTY_ZERO,
                cost_value_input=value,
                cost_rate_input=adjustment.unit_cost or MONEY_ZERO,
            )
        )

    for product_id, movements in movement_map.items():
        movement_map[product_id] = sorted(movements, key=_normalize_movement_order)

    return movement_map


def _inject_fallback_cost(state: ProductValuationState, missing_qty: Decimal) -> bool:
    fallback_rate = state.product.purchase_price_per_unit or MONEY_ZERO
    if missing_qty <= QTY_ZERO:
        return True
    if fallback_rate <= MONEY_ZERO:
        state.mark_incomplete("missing_opening_cost_basis")
        return False

    state.running_qty += missing_qty
    state.running_inventory_value += missing_qty * fallback_rate
    state.mark_estimated("opening_value_fallback_purchase_price")
    return True


def _consume_explicit_cost(state: ProductValuationState, qty_out: Decimal, cost_value: Decimal) -> None:
    if qty_out <= QTY_ZERO:
        return
    if state.running_qty < qty_out:
        if not _inject_fallback_cost(state, qty_out - state.running_qty):
            return

    state.running_qty -= qty_out
    state.running_inventory_value -= cost_value
    if state.running_inventory_value < MONEY_ZERO:
        state.mark_incomplete("inventory_value_below_zero")
        state.running_inventory_value = MONEY_ZERO
    if state.running_qty < QTY_ZERO:
        state.mark_incomplete("inventory_qty_below_zero")
        state.running_qty = QTY_ZERO


def _consume_sale_cost(state: ProductValuationState, qty_out: Decimal) -> Decimal:
    if qty_out <= QTY_ZERO:
        return MONEY_ZERO
    if state.running_qty < qty_out or state.average_cost() is None:
        missing_qty = qty_out - state.running_qty if state.running_qty < qty_out else qty_out
        if missing_qty > QTY_ZERO and not _inject_fallback_cost(state, missing_qty):
            return MONEY_ZERO
        if state.average_cost() is None:
            state.mark_incomplete("missing_average_cost_for_sale")
            return MONEY_ZERO

    average_cost = state.average_cost() or MONEY_ZERO
    cogs_value = qty_out * average_cost
    state.running_qty -= qty_out
    state.running_inventory_value -= cogs_value
    if state.running_inventory_value < MONEY_ZERO:
        state.mark_incomplete("inventory_value_below_zero")
        state.running_inventory_value = MONEY_ZERO
    if state.running_qty < QTY_ZERO:
        state.mark_incomplete("inventory_qty_below_zero")
        state.running_qty = QTY_ZERO
    return cogs_value


def _apply_movement(
    state: ProductValuationState,
    movement: ValuationMovement,
    *,
    in_period: bool,
) -> None:
    if movement.movement_type == "PURCHASE":
        if "charge allocation unresolved" in movement.reference:
            state.mark_estimated("purchase_charge_allocation_unresolved")
        state.running_qty += movement.qty_in
        state.running_inventory_value += movement.cost_value_input
        return

    if movement.movement_type == "STOCK_ADJ_UP":
        state.running_qty += movement.qty_in
        state.running_inventory_value += movement.cost_value_input
        return

    if movement.movement_type == "PURCHASE_RETURN":
        _consume_explicit_cost(state, movement.qty_out, movement.cost_value_input)
        return

    if movement.movement_type == "STOCK_ADJ_DOWN":
        _consume_explicit_cost(state, movement.qty_out, movement.cost_value_input)
        return

    if movement.movement_type == "SALE":
        cogs_value = _consume_sale_cost(state, movement.qty_out)
        if in_period:
            state.qty_sold += movement.qty_out
            state.gross_sales_revenue += movement.revenue_value
            state.sales_revenue += movement.revenue_value
            state.cogs += cogs_value
            state.gross_profit += movement.revenue_value - cogs_value
        return

    if movement.movement_type == "SALES_RETURN":
        average_cost_before_return = state.average_cost()
        if average_cost_before_return is None:
            if not _inject_fallback_cost(state, movement.qty_in):
                return
            average_cost_before_return = state.average_cost()
        reversal_cost = movement.qty_in * (average_cost_before_return or MONEY_ZERO)
        state.running_qty += movement.qty_in
        state.running_inventory_value += reversal_cost
        if in_period:
            state.qty_sold -= movement.qty_in
            state.sales_return_revenue += abs(movement.revenue_value)
            state.sales_revenue += movement.revenue_value
            state.cogs -= reversal_cost
            state.gross_profit += movement.revenue_value + reversal_cost
            state.mark_estimated("sales_return_reversed_at_running_average")


def _finalize_summary(state: ProductValuationState) -> Dict[str, Optional[Decimal]]:
    if state.confidence_label == CONFIDENCE_INCOMPLETE:
        cogs = None
        gross_profit = None
        gross_margin_pct = None
        avg_cost = None
        closing_value = None
    else:
        cogs = state.cogs
        gross_profit = state.gross_profit
        gross_margin_pct = (
            (gross_profit / state.sales_revenue) * Decimal("100")
            if state.sales_revenue not in (None, MONEY_ZERO)
            else None
        )
        avg_cost = (state.cogs / state.qty_sold) if state.qty_sold > QTY_ZERO else None
        closing_value = state.running_inventory_value

    avg_selling_price = (state.sales_revenue / state.qty_sold) if state.qty_sold > QTY_ZERO else None

    return {
        "product_id": state.product.id,
        "product_code": state.product.code,
        "product_name": f"{state.product.code} - {state.product.name}",
        "qty_sold": state.qty_sold,
        "gross_sales_revenue": state.gross_sales_revenue,
        "sales_return_revenue": state.sales_return_revenue,
        "return_rate_pct": (
            (state.sales_return_revenue / state.gross_sales_revenue) * Decimal("100")
            if state.gross_sales_revenue > MONEY_ZERO
            else None
        ),
        "sales_revenue": state.sales_revenue,
        "cogs": cogs,
        "gross_profit": gross_profit,
        "gross_margin_pct": gross_margin_pct,
        "avg_selling_price": avg_selling_price,
        "avg_cost": avg_cost,
        "closing_qty": state.running_qty,
        "closing_value": closing_value,
        "opening_qty": state.opening_qty,
        "opening_value": state.opening_value,
        "confidence_label": state.confidence_label,
    }


def build_product_profit_summaries(owner, date_from, date_to) -> List[Dict[str, Optional[Decimal]]]:
    products = Product.objects.filter(owner=owner)
    product_map = {product.id: product for product in products}
    movement_map = _build_product_movements(owner, date_to)

    summaries: List[Dict[str, Optional[Decimal]]] = []
    for product_id, movements in movement_map.items():
        product = product_map.get(product_id)
        if product is None:
            continue

        state = ProductValuationState(product=product)
        for movement in movements:
            in_period = movement.movement_date >= date_from
            _apply_movement(state, movement, in_period=in_period)
            if not in_period:
                state.opening_qty = state.running_qty
                state.opening_value = state.running_inventory_value

        if state.sales_revenue == MONEY_ZERO and state.qty_sold == QTY_ZERO and state.cogs == MONEY_ZERO:
            continue

        summaries.append(_finalize_summary(state))

    return summaries


def get_top_profitable_products(owner, date_from, date_to, limit: int = 10):
    summaries = build_product_profit_summaries(owner, date_from, date_to)
    ranked = sorted(
        summaries,
        key=lambda item: (
            item["gross_profit"] if item["gross_profit"] is not None else Decimal("-999999999999"),
            item["sales_revenue"],
        ),
        reverse=True,
    )
    return ranked[:limit]


def debug_product_profit_breakdown(owner, product_id, date_from, date_to):
    product = Product.objects.get(owner=owner, pk=product_id)
    movement_map = _build_product_movements(owner, date_to)
    movements = movement_map.get(product.id, [])
    state = ProductValuationState(product=product)
    rows = []

    for movement in movements:
        in_period = movement.movement_date >= date_from
        pre_qty = state.running_qty
        pre_inventory_value = state.running_inventory_value
        pre_average_cost = state.average_cost()
        pre_sales_revenue = state.sales_revenue
        pre_cogs = state.cogs
        pre_gross_profit = state.gross_profit
        pre_flags = set(state.flags)

        _apply_movement(state, movement, in_period=in_period)

        if not in_period:
            state.opening_qty = state.running_qty
            state.opening_value = state.running_inventory_value

        row_flags = sorted(state.flags - pre_flags)
        row_cogs_value = state.cogs - pre_cogs if in_period else MONEY_ZERO
        row_gross_profit_impact = state.gross_profit - pre_gross_profit if in_period else MONEY_ZERO
        row_sales_revenue_impact = state.sales_revenue - pre_sales_revenue if in_period else MONEY_ZERO

        rows.append(
            {
                "movement_date": movement.movement_date,
                "movement_type": movement.movement_type,
                "reference": movement.reference,
                "qty_in": movement.qty_in,
                "qty_out": movement.qty_out,
                "revenue_value": movement.revenue_value,
                "cost_value_input": movement.cost_value_input,
                "allocated_header_charge": movement.allocated_header_charge,
                "in_period": in_period,
                "pre_row_qty": pre_qty,
                "pre_row_inventory_value": pre_inventory_value,
                "pre_row_average_cost": pre_average_cost,
                "running_qty": state.running_qty,
                "running_inventory_value": state.running_inventory_value,
                "running_average_cost": state.average_cost(),
                "sales_revenue_impact": row_sales_revenue_impact,
                "cogs_value": row_cogs_value,
                "gross_profit_impact": row_gross_profit_impact,
                "confidence_flags_triggered": row_flags,
            }
        )

    summary = _finalize_summary(state)
    summary["opening_qty"] = state.opening_qty
    summary["opening_value"] = state.opening_value

    return {
        "product_id": product.id,
        "product_code": product.code,
        "product_name": product.name,
        "date_from": date_from,
        "date_to": date_to,
        "movements": rows,
        "summary": summary,
    }
