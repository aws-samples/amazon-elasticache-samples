# Macro Library: Returns, Marketplace, and Policy Window Responses (US)
**Macro Pack Version:** MAC-RET-US-v3.0

## Purpose
Reusable support messaging for return window questions, marketplace seller differences, and review/escalation language.

---

## Macro: RET_WINDOW_STANDARD_01
**Use when:** Item appears within standard return window and no exception complexity  
**Template:**
I can help with that return. Based on the order timing, the item appears to be within the applicable return window. I can walk you through the next steps based on how the item was purchased and fulfilled.

---

## Macro: RET_WINDOW_REVIEW_02
**Use when:** Item may be outside return window, but further review is appropriate  
**Template:**
I understand your request. Based on the order details I can see right now, the item may be outside the standard return window. If you would like, I can help review the order for any applicable exceptions or next-step options.

---

## Macro: MKT_SELLER_POLICY_DIFF_03
**Use when:** Seller-fulfilled marketplace order policy differs from retailer policy  
**Template:**
Because this item was sold and fulfilled by a marketplace seller, the return process and eligibility may differ from retailer-fulfilled items. I can help confirm the seller policy and any applicable marketplace protections for your order.

---

## Macro: REFUND_DELAY_STATUS_04
**Use when:** Refund was initiated but customer does not see posted funds yet  
**Template:**
Thanks for checking on this. Once a refund is processed, the posting time can vary by payment method and financial institution. I can confirm the refund status on our side and help explain what to expect next.

---

## Macro Guardrails
- Confirm fulfillment ownership before using marketplace wording
- Check temporary policy exceptions before quoting standard windows
- Use legal-approved wording for final denial or disputed claims
