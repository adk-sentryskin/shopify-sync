"""Encryption utilities for sensitive data"""
from cryptography.fernet import Fernet
from typing import Optional
import os


class TokenEncryption:
    """Handles encryption and decryption of access tokens"""

    def __init__(self, encryption_key: Optional[str] = None):
        if encryption_key is None:
            encryption_key = os.getenv("ENCRYPTION_KEY")

        if not encryption_key:
            raise ValueError(
                "ENCRYPTION_KEY environment variable is required for token encryption. "
                "Generate one using: python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
            )

        try:
            self.cipher = Fernet(encryption_key.encode() if isinstance(encryption_key, str) else encryption_key)
        except Exception as e:
            raise ValueError(f"Invalid encryption key format: {e}")

    def encrypt(self, plaintext: str) -> str:
        """Encrypt a plaintext string"""
        if not plaintext:
            return plaintext

        encrypted_bytes = self.cipher.encrypt(plaintext.encode())
        return encrypted_bytes.decode()

    def decrypt(self, encrypted_text: str) -> str:
        """Decrypt an encrypted string"""
        if not encrypted_text:
            return encrypted_text

        try:
            decrypted_bytes = self.cipher.decrypt(encrypted_text.encode())
            return decrypted_bytes.decode()
        except Exception as e:
            raise ValueError(f"Failed to decrypt token: {e}")


_encryption_instance: Optional[TokenEncryption] = None


def get_encryption() -> TokenEncryption:
    """Get or create the singleton TokenEncryption instance"""
    global _encryption_instance
    if _encryption_instance is None:
        _encryption_instance = TokenEncryption()
    return _encryption_instance
