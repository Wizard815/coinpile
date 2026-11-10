import os

from cryptography.fernet import Fernet

os.environ.setdefault("FIELD_KEY", Fernet.generate_key().decode())
os.environ.setdefault("UI_DIR", os.path.join(os.path.dirname(__file__), "..", "ui"))
