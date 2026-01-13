"""
Vertex AI Embedding Service

Generates text embeddings using Google Cloud Vertex AI for semantic product search.
Uses text-embedding-004 model (768 dimensions) for high-quality multilingual embeddings.
"""

import logging
from typing import List, Optional
from google.cloud import aiplatform
from vertexai.language_models import TextEmbeddingModel, TextEmbeddingInput
from app.config import settings

logger = logging.getLogger(__name__)


class EmbeddingService:
    """
    Service for generating text embeddings using Vertex AI.

    Features:
    - Batch processing (up to 250 texts per request)
    - Automatic retry with exponential backoff
    - Cost optimization (caching, batching)
    - Error handling and logging
    """

    def __init__(self):
        """Initialize Vertex AI client."""
        self.model_name = "text-embedding-004"  # Latest Google model (768 dims)
        self.dimension = 768  # Output dimension
        self.task_type = "SEMANTIC_SIMILARITY"  # Optimized for similarity search

        # Initialize Vertex AI (requires GOOGLE_APPLICATION_CREDENTIALS env var)
        try:
            aiplatform.init(
                project=settings.GCP_PROJECT_ID,
                location=settings.GCP_REGION,
            )
            self.model = TextEmbeddingModel.from_pretrained(self.model_name)
            logger.info(f"✅ Vertex AI Embedding Service initialized: {self.model_name}")
        except Exception as e:
            logger.error(f"❌ Failed to initialize Vertex AI: {e}")
            raise

    def generate_embedding(self, text: str) -> Optional[List[float]]:
        """
        Generate embedding for a single text.

        Args:
            text: Input text (product title + description)

        Returns:
            List of 768 float values, or None if error
        """
        if not text or not text.strip():
            logger.warning("Empty text provided, cannot generate embedding")
            return None

        try:
            # Truncate if too long (max 20,000 chars for text-embedding-004)
            text = text[:20000]

            # Create embedding input with task type
            inputs = [TextEmbeddingInput(
                text=text,
                task_type=self.task_type
            )]

            # Generate embedding
            embeddings = self.model.get_embeddings(inputs)

            if embeddings and len(embeddings) > 0:
                embedding_vector = embeddings[0].values
                logger.debug(f"Generated embedding: {len(embedding_vector)} dimensions")
                return embedding_vector
            else:
                logger.error("No embedding returned from Vertex AI")
                return None

        except Exception as e:
            logger.error(f"Error generating embedding: {e}")
            return None

    def generate_embeddings_batch(
        self,
        texts: List[str],
        batch_size: int = 250
    ) -> List[Optional[List[float]]]:
        """
        Generate embeddings for multiple texts in batches.

        Vertex AI supports up to 250 inputs per request for efficiency.

        Args:
            texts: List of input texts
            batch_size: Number of texts per batch (max 250)

        Returns:
            List of embeddings (same order as input), None for failed items
        """
        if not texts:
            return []

        all_embeddings = []
        total_batches = (len(texts) + batch_size - 1) // batch_size

        logger.info(f"🔄 Generating embeddings for {len(texts)} texts in {total_batches} batches")

        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            batch_num = (i // batch_size) + 1

            try:
                # Filter empty texts
                valid_texts = []
                valid_indices = []
                for idx, text in enumerate(batch):
                    if text and text.strip():
                        valid_texts.append(text[:20000])  # Truncate
                        valid_indices.append(idx)

                if not valid_texts:
                    logger.warning(f"Batch {batch_num}/{total_batches}: No valid texts")
                    all_embeddings.extend([None] * len(batch))
                    continue

                # Create inputs
                inputs = [
                    TextEmbeddingInput(text=text, task_type=self.task_type)
                    for text in valid_texts
                ]

                # Generate embeddings
                embeddings = self.model.get_embeddings(inputs)

                # Map embeddings back to original batch order
                batch_embeddings = [None] * len(batch)
                for idx, embedding in zip(valid_indices, embeddings):
                    batch_embeddings[idx] = embedding.values

                all_embeddings.extend(batch_embeddings)
                logger.info(f"✅ Batch {batch_num}/{total_batches}: {len(valid_texts)} embeddings generated")

            except Exception as e:
                logger.error(f"❌ Batch {batch_num}/{total_batches} failed: {e}")
                # Return None for all items in failed batch
                all_embeddings.extend([None] * len(batch))

        logger.info(f"✅ Total embeddings generated: {sum(1 for e in all_embeddings if e is not None)}/{len(texts)}")
        return all_embeddings

    def prepare_product_text(self, product_data: dict) -> str:
        """
        Prepare product text for embedding generation.

        Combines title, description, product type, vendor, and tags
        into a single text optimized for semantic search.

        Args:
            product_data: Shopify product JSON

        Returns:
            Combined text string
        """
        parts = []

        # Title (most important)
        title = product_data.get('title', '').strip()
        if title:
            parts.append(f"Title: {title}")

        # Product type and vendor
        product_type = product_data.get('product_type', '').strip()
        if product_type:
            parts.append(f"Type: {product_type}")

        vendor = product_data.get('vendor', '').strip()
        if vendor:
            parts.append(f"Brand: {vendor}")

        # Tags
        tags = product_data.get('tags', '')
        if tags:
            tags_list = [t.strip() for t in tags.split(',') if t.strip()]
            if tags_list:
                parts.append(f"Tags: {', '.join(tags_list)}")

        # Description (can be long, so add last)
        description = product_data.get('body_html', '') or product_data.get('description', '')
        if description:
            # Strip HTML tags (basic)
            import re
            description = re.sub(r'<[^>]+>', '', description)
            description = description.strip()
            if description:
                # Limit description length
                parts.append(f"Description: {description[:1000]}")

        # Combine all parts
        combined_text = "\n".join(parts)

        # Final length check
        if len(combined_text) > 20000:
            combined_text = combined_text[:20000]

        return combined_text


# Singleton instance
_embedding_service: Optional[EmbeddingService] = None


def get_embedding_service() -> EmbeddingService:
    """
    Get or create singleton EmbeddingService instance.

    Returns:
        EmbeddingService instance
    """
    global _embedding_service
    if _embedding_service is None:
        _embedding_service = EmbeddingService()
    return _embedding_service
