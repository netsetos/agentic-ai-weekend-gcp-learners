"""Answer the question you already answered."""
import os

from google.cloud import firestore
from google.cloud.firestore_v1.base_vector_query import DistanceMeasure
from google.cloud.firestore_v1.vector import Vector

THRESHOLD = float(os.environ.get("SEMANTIC_CACHE_THRESHOLD", "0.92"))
TTL_HOURS = int(os.environ.get("SEMANTIC_CACHE_TTL_H", "24"))


def lookup(db: firestore.Client, tenant_id: str, qvec: list[float]) -> dict | None:
    """Nearest previous question for THIS tenant, if it is near enough.

    Per tenant, always. A cache keyed on the question alone would serve one
    customer's answer to another - and it would look like a performance win
    right up until someone noticed.
    """
    hits = (db.collection("answer_cache")
              .where("tenant_id", "==", tenant_id)
              .find_nearest("embedding", Vector(qvec),
                            distance_measure=DistanceMeasure.COSINE,
                            limit=1,
                            distance_result_field="d").get())
    for h in hits:
        d = h.to_dict()
        # COSINE distance: smaller is closer. 1 - d is the similarity.
        if (1.0 - d.get("d", 1.0)) >= THRESHOLD:
            return d
    return None


def store(db: firestore.Client, tenant_id: str, question: str,
          qvec: list[float], answer: dict) -> None:
    db.collection("answer_cache").add({
        "tenant_id": tenant_id, "question": question,
        "embedding": Vector(qvec), "answer": answer,
        "created_at": firestore.SERVER_TIMESTAMP,
    })
