"""QR code generation tools."""

import io
from collections import deque
import qrcode
from strands import tool

# Global queue for pending images
_pending_images = deque()


@tool
def generate_qr_code(title: str, data: str) -> str:
    """
    Generate a QR code for the provided data (text or URL). Use this tool primarily to generate
    google maps links to specific locations, for example to help Amazonians navigate to IT resources.

    Example:
        https://www.google.com/maps/search/?api=1&query=47.6155,-122.3419,17 drops a pin the spheres that a user could nav to
    Replace the lat/long with the relevant location you want to tell the amazonian about. When showing the QR code, also tell
    the user the actual address if available, and where in the building the IT Service can be found

    Args:
        title: A descriptive title for the QR code explaining its purpose
               Examples:
               - "SEA25 - Obidos IT Office"
               - "Navigate to IT Vending Machine"
               - "IT Support Location at Midway"
               - "DFW7 Building Directory"
        data: The text or URL to encode in the QR code

    Returns:
        str: Confirmation message
    """
    # Generate QR code
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(data)
    qr.make(fit=True)

    # Create image
    img = qr.make_image(fill_color="black", back_color="white")

    # Convert to bytes
    img_bytes = io.BytesIO()
    img.save(img_bytes, format='PNG')
    img_bytes.seek(0)

    # Dump to queue
    _pending_images.append({
        "type": "qr_code",
        "content": img_bytes.read(),
        "description": f"QR code for {title}: {data}"
    })

    return f"✅ Generated QR code for {title}"


def get_pending_images():
    """
    Get all pending images and clear the queue.

    Returns:
        list: List of pending image dictionaries
    """
    images = list(_pending_images)
    _pending_images.clear()
    return images
