from pyramid.security import Authenticated
from pyramid.settings import asbool

from kinto.core import resource, utils
from kinto.core.errors import raise_invalid
from kinto.views import object_exists_or_404
from kinto.schema_validation import (validate_from_bucket_schema_or_400, validate_schema,
                                     ValidationError, RefResolutionError)


_parent_path = '/buckets/{{bucket_id}}/collections/{{collection_id}}'


@resource.register(name='record',
                   collection_path=_parent_path + '/records',
                   record_path=_parent_path + '/records/{{id}}')
class Record(resource.ShareableResource):

    schema_field = 'schema'

    def __init__(self, request, **kwargs):
        # Before all, first check that the parent collection exists.
        # Check if already fetched before (in batch).
        collections = request.bound_data.setdefault('collections', {})
        collection_uri = self.get_parent_id(request)
        if collection_uri not in collections:
            # Unknown yet, fetch from storage.
            bucket_uri = utils.instance_uri(request, 'bucket', id=self.bucket_id)
            collection = object_exists_or_404(request,
                                              collection_id='collection',
                                              parent_id=bucket_uri,
                                              object_id=self.collection_id)
            collections[collection_uri] = collection
        self._collection = collections[collection_uri]

        super().__init__(request, **kwargs)

    def get_parent_id(self, request):
        self.bucket_id = request.matchdict['bucket_id']
        self.collection_id = request.matchdict['collection_id']
        return utils.instance_uri(request, 'collection',
                                  bucket_id=self.bucket_id,
                                  id=self.collection_id)

    def process_record(self, new, old=None):
        """Validate records against collection or bucket schema, if any."""
        new = super().process_record(new, old)

        # Is schema validation enabled?
        settings = self.request.registry.settings
        schema_validation = 'experimental_collection_schema_validation'
        if not asbool(settings.get(schema_validation)):
            return new

        # Remove internal and auto-assigned fields from schemas and record.
        internal_fields = (self.model.id_field,
                           self.model.modified_field,
                           self.schema_field,
                           self.model.permissions_field)

        # The schema defined on the collection will be validated first.
        if 'schema' in self._collection:
            schema = self._collection['schema']

            try:
                validate_schema(new, schema, ignore_fields=internal_fields)
            except ValidationError as e:
                raise_invalid(self.request, name=e.field, description=e.message)
            except RefResolutionError as e:
                raise_invalid(self.request, name='schema', description=str(e))

            # Assign the schema version to the record.
            schema_timestamp = self._collection[self.model.modified_field]
            new[self.schema_field] = schema_timestamp

        # Validate also from the record:schema field defined on the bucket.
        validate_from_bucket_schema_or_400(new, resource_name="record", request=self.request,
                                           ignore_fields=internal_fields)

        return new

    def collection_get(self):
        result = super().collection_get()
        self._handle_cache_expires(self.request.response)
        return result

    def get(self):
        result = super().get()
        self._handle_cache_expires(self.request.response)
        return result

    def _handle_cache_expires(self, response):
        """If the parent collection defines a ``cache_expires`` attribute,
        then cache-control response headers are sent.

        .. note::

            Those headers are also sent if the
            ``kinto.record_cache_expires_seconds`` setting is defined.
        """
        is_anonymous = Authenticated not in self.request.effective_principals
        if not is_anonymous:
            return

        cache_expires = self._collection.get('cache_expires')
        if cache_expires is None:
            by_bucket = '{}_record_cache_expires_seconds'.format(self.bucket_id)
            by_collection = '{}_{}_record_cache_expires_seconds'.format(
                self.bucket_id, self.collection_id)
            settings = self.request.registry.settings
            cache_expires = settings.get(by_collection,
                                         settings.get(by_bucket))

        if cache_expires is not None:
            response.cache_expires(seconds=int(cache_expires))
