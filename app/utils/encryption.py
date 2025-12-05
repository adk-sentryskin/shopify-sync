"""
Encryption utilities for sensitive data like access tokens
Uses Fernet (symmetric encryption) from the cryptography library
"""
from cryptography.fernet import Fernet
from typing import Optional
import base64
import os


class TokenEncryption:
    """
    Handles encryption and decryption of access tokens
    """

    def __init__(self, encryption_key: Optional[str] = None):
        """
        Initialize with encryption key from environment or generate a new one

        Args:
            encryption_key: Base64-encoded Fernet key. If None, uses ENCRYPTION_KEY env var
        """
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
        """
        Encrypt a plaintext string

        Args:
            plaintext: The string to encrypt

        Returns:
            Base64-encoded encrypted string
        """
        if not plaintext:
            return plaintext

        encrypted_bytes = self.cipher.encrypt(plaintext.encode())
        return encrypted_bytes.decode()

    def decrypt(self, encrypted_text: str) -> str:
        """
        Decrypt an encrypted string

        Args:
            encrypted_text: Base64-encoded encrypted string

        Returns:
            Decrypted plaintext string
        """
        if not encrypted_text:
            return encrypted_text

        try:
            decrypted_bytes = self.cipher.decrypt(encrypted_text.encode())
            return decrypted_bytes.decode()
        except Exception as e:
            raise ValueError(f"Failed to decrypt token: {e}")


# Singleton instance
_encryption_instance: Optional[TokenEncryption] = None


def get_encryption() -> TokenEncryption:
    """
    Get or create the singleton TokenEncryption instance

    Returns:
        TokenEncryption instance
    """
    global _encryption_instance
    if _encryption_instance is None:
        _encryption_instance = TokenEncryption()
    return _encryption_instance
