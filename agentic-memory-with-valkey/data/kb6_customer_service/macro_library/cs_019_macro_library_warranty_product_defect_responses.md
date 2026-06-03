# Macro Library: Warranty and Product Defect Responses

**Macro Pack Version:** MAC-WAR-v1.0
**Status:** Approved
**Last Updated:** 2026-02-22

---

### MACRO: Footwear Warranty Claim — Photo Request
**ID:** WAR-001
**Channel:** Chat / Email

> Hi [Customer Name],
>
> I'm sorry to hear about the issue with your [Product Name]. That definitely doesn't sound right, and I'd like to help get this resolved.
>
> To process a warranty claim, could you send me a couple of photos showing the defect? A close-up of the affected area and one showing the full shoe would be perfect. You can reply to this message with the photos attached.
>
> Once I review them, I'll get your replacement set up right away.
>
> [Agent Name]

---

### MACRO: Warranty Approved — Replacement Shipping
**ID:** WAR-002
**Channel:** Chat / Email

> Hi [Customer Name],
>
> I've reviewed the photos and confirmed this is a manufacturing defect covered under warranty. I'm setting up a replacement [Product Name] in size [Size] to ship to you.
>
> Here's what happens next:
> - Your replacement will ship within 3–5 business days
> - You'll receive a prepaid shipping label to return the defective item
> - No charge to you
>
> Is the same size and color okay, or would you like a different option?
>
> [Agent Name]

**Note:** If the product is out of stock, consult the substitution playbook (KB1: pk_006) for approved alternatives before offering.

---

### MACRO: Warranty Denied — Out of Warranty
**ID:** WAR-003
**Channel:** Chat / Email
**Legal Approved:** Yes (pc_005)

> Hi [Customer Name],
>
> Thank you for sending those photos. I can see the wear on your [Product Name], and I understand that's frustrating.
>
> Unfortunately, this type of wear falls outside our warranty coverage, which covers manufacturing defects within [1 year / 90 days for GearVault] of purchase. Based on the photos, this appears to be normal wear from use rather than a manufacturing issue.
>
> I'd like to help you find a great replacement though! [Offer current promotions, loyalty discount, or substitution recommendation from KB1: pk_006]
>
> [Agent Name]

---

### MACRO: Warranty Denied — Counterfeit Product
**ID:** WAR-004
**Channel:** Chat / Email
**Legal Approved:** Yes (pc_024)

> Hi [Customer Name],
>
> I've looked into your order, and I need to let you know that the product you received was not manufactured by Accent Athletics and does not meet our authenticity standards. Because of this, it isn't covered under our warranty program.
>
> However, I can process a full refund for your order right now. I can also help you find the genuine version of the product if you're interested.
>
> [Agent Name]

---

### MACRO: SwiftPace Bricking — Expedited Replacement
**ID:** WAR-005
**Channel:** Chat / Email

> Hi [Customer Name],
>
> I'm sorry the recovery steps didn't work for your SwiftPace watch. Since this was caused by our firmware update, I'm setting up an expedited replacement at no cost to you — it will ship tomorrow.
>
> You don't need to return the affected watch. Your activity data and settings are saved in your cloud account and will sync to the new watch automatically.
>
> Is your current shipping address still [Address]?
>
> [Agent Name]

**Policy:** Per pc_020 temporary exception — no warranty check needed, no return required, expedited shipping.
