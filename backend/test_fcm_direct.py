import os
import sys

# Point to backend directory
sys.path.insert(0, r"C:\Users\UnoTEAM-0144\Documents\MoldflowMobileSystem\backend")
import fcm_service

os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = r"C:\MF\MoldflowSynergyPlugin\secrets\moldflow-mobile-firebase-adminsdk-fbsvc-fb332a383b.json"

token = "fJZ0K79gQKGgrk_Gj5F9hg:APA91bFh_3a94DFtig3alFk9p-HUkmQBRiHbcFjdzuesaI-JBE9OoSj0RIyhimCSB-4jnrO2VC0GC--8b6uyIlhy5mp-qA-MZV-m6_mATulDcIg0a5fqx7o"

print(f"Testing FCM send to token: {token[:25]}...")
try:
    fcm_service.send_fcm_notification(
        device_token=token,
        title="Diagnostic Test Notification",
        body="Moldflow Mobile notification diagnostic probe",
        data={"diagnostic": "true", "timestamp": "now"}
    )
    print("SUCCESS: Notification sent!")
except fcm_service.UnregisteredDeviceError as e:
    print(f"FAILED (UnregisteredDeviceError): {e}")
except Exception as e:
    print(f"FAILED: {e}")
