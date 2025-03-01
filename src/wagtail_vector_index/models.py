from typing import Iterable, List

from django.contrib.contenttypes.fields import GenericForeignKey, GenericRelation
from django.contrib.contenttypes.models import ContentType
from django.core import checks
from django.core.exceptions import FieldDoesNotExist
from django.db import models, transaction
from every_ai import EmbeddingAbility
from langchain.text_splitter import RecursiveCharacterTextSplitter
from wagtail.models import Page
from wagtail.search.index import BaseField

from wagtail_vector_index.index.base import Document
from wagtail_vector_index.index.exceptions import IndexedTypeFromDocumentError
from wagtail_vector_index.index.model import (
    ModelVectorIndex,
    PageVectorIndex,
)

EMBEDDING_SPLIT_LENGTH_CHARS = 800
EMBEDDING_SPLIT_OVERLAP_CHARS = 100


class Embedding(models.Model):
    """Stores an embedding for a model instance"""

    content_type = models.ForeignKey(
        ContentType, on_delete=models.CASCADE, related_name="+"
    )
    base_content_type = models.ForeignKey(
        ContentType, on_delete=models.CASCADE, related_name="+"
    )
    object_id = models.CharField(
        max_length=255,
    )
    content_object = GenericForeignKey(
        "content_type", "object_id", for_concrete_model=False
    )
    vector = models.JSONField()
    content = models.TextField()

    def __str__(self):
        return f"Embedding for {self.object_id}"

    @classmethod
    def _get_base_content_type(cls, model_or_object):
        if parents := model_or_object._meta.get_parent_list():
            return ContentType.objects.get_for_model(
                parents[-1], for_concrete_model=False
            )
        else:
            return ContentType.objects.get_for_model(
                model_or_object, for_concrete_model=False
            )

    @classmethod
    def from_instance(cls, instance: models.Model) -> "Embedding":
        """Create an Embedding instance for a model instance"""
        content_type = ContentType.objects.get_for_model(instance)
        return Embedding(
            content_type=content_type,
            base_content_type=cls._get_base_content_type(instance),
            object_id=instance.pk,
        )

    @classmethod
    def get_for_instance(cls, instance: models.Model):
        """Get all Embedding instances that are related to a model instance"""
        content_type = ContentType.objects.get_for_model(instance)
        return Embedding.objects.filter(
            content_type=content_type, object_id=instance.pk
        )


class EmbeddingField(BaseField):
    """A field that can be used to specify which fields of a model should be used to generate embeddings"""

    def __init__(self, *args, important=False, **kwargs):
        self.important = important
        super().__init__(*args, **kwargs)


class VectorIndexedMixin(models.Model):
    """Mixin for Django models that make them conform to the VectorIndexable protocol and stores
    embeddings in an Embedding model"""

    embedding_fields = []
    embeddings = GenericRelation(
        Embedding, content_type_field="content_type", for_concrete_model=False
    )
    vector_index_class = None

    class Meta:
        abstract = True

    @classmethod
    def _get_embedding_fields(cls) -> List["EmbeddingField"]:
        embedding_fields = {
            (type(field), field.field_name): field for field in cls.embedding_fields
        }
        return list(embedding_fields.values())

    def _get_split_content(
        self,
        *,
        split_length=EMBEDDING_SPLIT_LENGTH_CHARS,
        split_overlap=EMBEDDING_SPLIT_OVERLAP_CHARS,
    ) -> List[str]:
        """Split the contents of a model instance's `embedding_fields` in to smaller chunks"""
        splittable_content = []
        important_content = []
        embedding_fields = self._meta.model._get_embedding_fields()

        for field in embedding_fields:
            value = field.get_value(self)
            final_value = value if isinstance(value, str) else "\n".join(value)
            if field.important:
                important_content.append(final_value)
            else:
                splittable_content.append(final_value)

        text = "\n".join(splittable_content)
        important_text = "\n".join(important_content)
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=split_length,
            chunk_overlap=split_overlap,
        )
        return [f"{important_text}\n{text}" for text in splitter.split_text(text)]

    @classmethod
    def check(cls, **kwargs):
        """Extend model checks to include validation of embedding_fields in the
        same way that Wagtail's Indexed class does it."""
        errors = super().check(**kwargs)
        errors.extend(cls._check_embedding_fields(**kwargs))
        return errors

    @classmethod
    def _has_field(cls, name):
        try:
            cls._meta.get_field(name)
        except FieldDoesNotExist:
            return hasattr(cls, name)
        else:
            return True

    @classmethod
    def _check_embedding_fields(cls, **kwargs):
        errors = []
        for field in cls._get_embedding_fields():
            message = "{model}.embedding_fields contains non-existent field '{name}'"
            if not cls._has_field(field.field_name):
                errors.append(
                    checks.Warning(
                        message.format(model=cls.__name__, name=field.field_name),
                        obj=cls,
                        id="wagtailai.WA001",
                    )
                )
        return errors

    def _existing_embeddings_match(
        self, embeddings: Iterable[Embedding], splits: List[str]
    ) -> bool:
        """Determine whether the embeddings passed in match the text content passed in"""
        if not embeddings:
            return False

        embedding_content = {embedding.content for embedding in embeddings}

        return set(splits) == embedding_content

    @transaction.atomic
    def _to_embeddings(self, ai_backend: EmbeddingAbility) -> List[Embedding]:
        """Use the AI backend to generate and store embeddings for this object"""
        splits = self._get_split_content()
        embeddings = Embedding.get_for_instance(self)

        # If the existing embeddings all match on content, we return them
        # without generating new ones
        if self._existing_embeddings_match(embeddings, splits):
            return list(embeddings)

        # Otherwise we delete all the existing embeddings and get new ones
        embeddings.delete()

        embedding_vectors = ai_backend.embed(splits)
        generated_embeddings: List[Embedding] = []
        for idx, split in enumerate(splits):
            embedding = Embedding.from_instance(self)
            embedding.vector = embedding_vectors[idx]
            embedding.content = split
            embedding.save()
            generated_embeddings.append(embedding)

        return generated_embeddings

    def to_documents(self, *, ai_backend: EmbeddingAbility):
        embeddings = self._to_embeddings(ai_backend=ai_backend)
        return [
            Document(
                id=str(embedding.pk),
                vector=embedding.vector,
                metadata={
                    "object_id": str(embedding.object_id),
                    "content_type_id": str(embedding.content_type_id),
                    "content": embedding.content,
                },
            )
            for embedding in embeddings
        ]

    @classmethod
    def from_document(cls, document):
        if obj := cls.objects.filter(
            pk=document.metadata["object_id"],
            content_type=document.metadata["content_type_id"],
        ).first():
            return obj
        else:
            raise IndexedTypeFromDocumentError("No object found for document")

    @classmethod
    def bulk_to_documents(cls, objects, *, ai_backend: EmbeddingAbility):
        # TODO: Implement a more efficient bulk embedding approach
        for object in objects:
            yield object.to_documents(ai_backend=ai_backend)

    @classmethod
    def bulk_from_documents(cls, documents):
        # TODO: Implement a more efficient approach
        for document in documents:
            yield cls.from_document(document)

    @classmethod
    def get_vector_index(cls):
        """Get a vector index instance for this model"""

        # If the user has specified a custom `vector_index_class`, use that
        if cls.vector_index_class:
            index_cls = cls.vector_index_class
        # If the model is a Wagtail Page, use a special PageVectorIndex
        elif issubclass(cls, Page):
            index_cls = PageVectorIndex
        # Otherwise use the standard ModelVectorIndex
        else:
            index_cls = ModelVectorIndex

        return type(
            f"{cls.__name__}Index",
            (index_cls,),
            {"querysets": [cls.objects.all()]},
        )(object_type=cls)
