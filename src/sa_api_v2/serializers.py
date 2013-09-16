"""
DjangoRestFramework resources for the Shareabouts REST API.
"""
import ujson as json
from itertools import groupby
from django.contrib.gis.geos import GEOSGeometry
from django.db.models import Count
from rest_framework import pagination
from rest_framework import serializers
from rest_framework.reverse import reverse

from . import models
from . import utils
from . import cache
from .params import (INCLUDE_INVISIBLE_PARAM, INCLUDE_PRIVATE_PARAM,
    INCLUDE_SUBMISSIONS_PARAM, FORMAT_PARAM)


###############################################################################
#
# Geo-related fields
# ------------------
#

class GeometryField(serializers.WritableField):
    def __init__(self, format='dict', *args, **kwargs):
        self.format = format

        if self.format not in ('json', 'wkt'):
            raise ValueError('Invalid format: %s' % self.format)

        super(GeometryField, self).__init__(*args, **kwargs)

    def to_native(self, obj):
        if self.format == 'json':
            return obj.json
        elif self.format == 'wkt':
            return obj.wkt
        else:
            raise ValueError('Cannot output as %s' % self.format)

    def from_native(self, data):
        if not isinstance(data, basestring):
            data = json.dumps(data)
        return GEOSGeometry(data)


###############################################################################
#
# Shareabouts-specific fields
# ---------------------------
#

class ShareaboutsFieldMixin (object):

    # These names should match the names of the cache parameters, and should be
    # in the same order as the corresponding URL arguments.
    url_arg_names = ()

    def get_url_kwargs(self, obj):
        """
        Pull the appropriate arguments off of the cache to construct the URL.
        """
        if isinstance(obj, models.User):
            instance_kwargs = {'owner_username': obj.username}
        else:
            instance_kwargs = obj.cache.get_cached_instance_params(obj.pk, lambda: obj)

        url_kwargs = {}
        for arg_name in self.url_arg_names:
            arg_value = instance_kwargs.get(arg_name, None)
            if arg_value is None:
                try:
                    arg_value = getattr(obj, arg_name)
                except AttributeError:
                    raise KeyError('No arg named %r in %r' % (arg_name, instance_kwargs))
            url_kwargs[arg_name] = arg_value
        return url_kwargs


class ShareaboutsRelatedField (ShareaboutsFieldMixin, serializers.HyperlinkedRelatedField):
    """
    Represents a Shareabouts relationship using hyperlinking.
    """
    read_only = True
    view_name = None

    def __init__(self, *args, **kwargs):
        if self.view_name is not None:
            kwargs['view_name'] = self.view_name
        super(ShareaboutsRelatedField, self).__init__(*args, **kwargs)

    def to_native(self, obj):
        view_name = self.view_name
        request = self.context.get('request', None)
        format = self.format or self.context.get('format', None)

        pk = getattr(obj, 'pk', None)
        if pk is None:
            return

        kwargs = self.get_url_kwargs(obj)
        return reverse(view_name, kwargs=kwargs, request=request, format=format)


class DataSetRelatedField (ShareaboutsRelatedField):
    view_name = 'dataset-detail'
    url_arg_names = ('owner_username', 'dataset_slug')


class DataSetKeysRelatedField (ShareaboutsRelatedField):
    view_name = 'apikey-list'
    url_arg_names = ('owner_username', 'dataset_slug')


class UserRelatedField (ShareaboutsRelatedField):
    view_name = 'user-detail'
    url_arg_names = ('owner_username',)


class PlaceRelatedField (ShareaboutsRelatedField):
    view_name = 'place-detail'
    url_arg_names = ('owner_username', 'dataset_slug', 'place_id')


class SubmissionSetRelatedField (ShareaboutsRelatedField):
    view_name = 'submission-list'
    url_arg_names = ('owner_username', 'dataset_slug', 'place_id', 'submission_set_name')


class ShareaboutsIdentityField (ShareaboutsFieldMixin, serializers.HyperlinkedIdentityField):
    read_only = True

    def __init__(self, *args, **kwargs):
        view_name = kwargs.pop('view_name', None) or getattr(self, 'view_name', None)
        super(ShareaboutsIdentityField, self).__init__(view_name=view_name, *args, **kwargs)

    def field_to_native(self, obj, field_name):
        request = self.context.get('request', None)
        format = self.context.get('format', None)
        view_name = self.view_name or self.parent.opts.view_name

        kwargs = self.get_url_kwargs(obj)

        if format and self.format and self.format != format:
            format = self.format

        return reverse(view_name, kwargs=kwargs, request=request, format=format)


class PlaceIdentityField (ShareaboutsIdentityField):
    url_arg_names = ('owner_username', 'dataset_slug', 'place_id')


class SubmissionSetIdentityField (ShareaboutsIdentityField):
    url_arg_names = ('owner_username', 'dataset_slug', 'place_id', 'submission_set_name')
    view_name = 'submission-list'


class DataSetPlaceSetIdentityField (ShareaboutsIdentityField):
    url_arg_names = ('owner_username', 'dataset_slug')
    view_name = 'place-list'


class DataSetSubmissionSetIdentityField (ShareaboutsIdentityField):
    url_arg_names = ('owner_username', 'dataset_slug', 'submission_set_name')
    view_name = 'dataset-submission-list'


class SubmissionIdentityField (ShareaboutsIdentityField):
    url_arg_names = ('owner_username', 'dataset_slug', 'place_id', 'submission_set_name', 'submission_id')


class DataSetIdentityField (ShareaboutsIdentityField):
    url_arg_names = ('owner_username', 'dataset_slug')


class AttachmentSerializer (serializers.ModelSerializer):
    class Meta:
        model = models.Attachment
        exclude = ('id', 'thing',)

###############################################################################
#
# Serializer Mixins
# -----------------
#


class DataBlobProcessor (object):
    """
    Like ModelSerializer, but automatically serializes/deserializes a
    'data' JSON blob of arbitrary key/value pairs.
    """

    def convert_object(self, obj):
        attrs = super(DataBlobProcessor, self).convert_object(obj)

        data = json.loads(obj.data)
        del attrs['data']
        attrs.update(data)

        return attrs

    def restore_fields(self, data, files):
        """
        Converts a dictionary of data into a dictionary of deserialized fields.
        """
        model = self.opts.model
        blob = {}
        data_copy = {}

        # Pull off any fields that the model doesn't know about directly
        # and put them into the data blob.
        known_fields = set(model._meta.get_all_field_names())

        # Also ignore the following field names (treat them like reserved
        # words).
        known_fields.update(self.fields.keys())

        # And allow an arbitrary value field named 'data' (don't let the
        # data blob get in the way).
        known_fields.remove('data')

        for key in data:
            if key in known_fields:
                data_copy[key] = data[key]
            else:
                blob[key] = data[key]

        data_copy['data'] = json.dumps(blob)

        if not self.partial:
            for field_name, field in self.fields.items():
                if (not field.read_only and field_name not in data_copy):
                    data_copy[field_name] = field.default

        return super(DataBlobProcessor, self).restore_fields(data_copy, files)

    def to_native(self, obj):
        data = super(DataBlobProcessor, self).to_native(obj)
        blob = data.pop('data')

        blob_data = json.loads(blob)
        request = self.context['request']

        # Did the user not ask for private data? Remove it!
        if INCLUDE_PRIVATE_PARAM not in request.GET:
            for key in blob_data.keys():
                if key.startswith('private'):
                    del blob_data[key]

        data.update(blob_data)
        return data


class CachedSerializer (object):
    def to_native(self, obj):
        cache = self.opts.model.cache
        cache_params = self.get_cache_params()
        data_getter = lambda: super(CachedSerializer, self).to_native(obj)

        data = cache.get_serialized_data(obj, data_getter, **cache_params)
        return data

    def get_cache_params(self):
        """
        Get a dictionary of flags that determine the contents of the place's
        submission sets. These flags are used primarily to determine the cache
        key for the submission sets structure.
        """
        request = self.context['request']
        request_params = dict(request.GET.iterlists())

        params = {
            'include_submissions': INCLUDE_SUBMISSIONS_PARAM in request_params,
            'include_private': INCLUDE_PRIVATE_PARAM in request_params,
            'include_invisible': INCLUDE_INVISIBLE_PARAM in request_params,
        }

        request_params.pop(INCLUDE_SUBMISSIONS_PARAM, None)
        request_params.pop(INCLUDE_PRIVATE_PARAM, None)
        request_params.pop(INCLUDE_INVISIBLE_PARAM, None)
        request_params.pop(FORMAT_PARAM, None)

        # If this doesn't have a parent serializer, then use all the rest of the
        # query parameters
        if self.parent is None:
            params.update(request_params)

        return params


###############################################################################
#
# Serializers
# -----------
#

class SubmissionSetSummarySerializer (CachedSerializer, serializers.HyperlinkedModelSerializer):
    length = serializers.IntegerField()
    url = SubmissionSetIdentityField()

    class Meta:
        model = models.SubmissionSet
        fields = ('length', 'url')


class DataSetPlaceSetSummarySerializer (serializers.HyperlinkedModelSerializer):
    length = serializers.IntegerField(source='places_length')
    url = DataSetPlaceSetIdentityField()

    class Meta:
        model = models.DataSet
        fields = ('length', 'url')

    def to_native(self, obj):
        place_count_map = self.context['place_count_map_getter']()
        obj.places_length = place_count_map.get(obj.pk, 0)
        data = super(DataSetPlaceSetSummarySerializer, self).to_native(obj)
        return data


class DataSetSubmissionSetSummarySerializer (serializers.HyperlinkedModelSerializer):
    length = serializers.IntegerField(source='submission_set_length')
    url = DataSetSubmissionSetIdentityField()

    class Meta:
        model = models.DataSet
        fields = ('length', 'url')

    def to_native(self, obj):
        submission_sets_map = self.context['submission_sets_map_getter']()
        sets = submission_sets_map.get(obj.id, {})
        summaries = {}
        for submission_set in sets:
            set_name = submission_set['parent__name']
            obj.submission_set_name = set_name
            obj.submission_set_length = submission_set['length']
            summaries[set_name] = super(DataSetSubmissionSetSummarySerializer, self).to_native(obj)
        return summaries


class PlaceSerializer (CachedSerializer, DataBlobProcessor, serializers.HyperlinkedModelSerializer):
    url = PlaceIdentityField()
    geometry = GeometryField(format='wkt')
    dataset = DataSetRelatedField()
    attachments = AttachmentSerializer(read_only=True)

    class Meta:
        model = models.Place

    def get_submission_set_summaries(self, obj):
        """
        Get a mapping from place id to a submission set summary dictionary.
        Get this for the entire dataset at once.
        """
        request = self.context['request']
        include_invisible = INCLUDE_INVISIBLE_PARAM in request.GET

        summaries = {}
        for submission_set in obj.submission_sets.all():
            submissions = submission_set.children.all()
            if not include_invisible:
                submissions = filter(lambda s: s.visible, submissions)
            submission_set.length = len(submissions)

            if submission_set.length == 0:
                continue

            serializer = SubmissionSetSummarySerializer(submission_set, context=self.context)
            serializer.parent = self
            summaries[submission_set.name] = serializer.data

        return summaries

    def get_detailed_submission_sets(self, obj):
        """
        Get a mapping from place id to a detiled submission set dictionary.
        Get this for the entire dataset at once.
        """
        request = self.context['request']
        include_invisible = INCLUDE_INVISIBLE_PARAM in request.GET

        details = {}
        for submission_set in obj.submission_sets.all():
            submissions = submission_set.children.all()
            if not include_invisible:
                submissions = filter(lambda s: s.visible, submissions)

            if len(submissions) == 0:
                continue

            # We know that the submission datasets will be the same as the place
            # dataset, so say so and avoid an extra query for each.
            for submission in submissions:
                submission.dataset = obj.dataset

            serializer = SubmissionSerializer(submissions, context=self.context)
            serializer.parent = self
            details[submission_set.name] = serializer.data

        return details

    def to_native(self, obj):
        data = super(PlaceSerializer, self).to_native(obj)
        request = self.context['request']

        if INCLUDE_SUBMISSIONS_PARAM not in request.GET:
            submission_sets_getter = self.get_submission_set_summaries
        else:
            submission_sets_getter = self.get_detailed_submission_sets

        data['submission_sets'] = submission_sets_getter(obj)

        if hasattr(obj, 'distance'):
            data['distance'] = str(obj.distance)

        return data


class SubmissionSerializer (CachedSerializer, DataBlobProcessor, serializers.HyperlinkedModelSerializer):
    url = SubmissionIdentityField()
    dataset = DataSetRelatedField()
    set = SubmissionSetRelatedField(source='parent')
    place = PlaceRelatedField(source='parent.place')
    attachments = AttachmentSerializer(read_only=True)

    class Meta:
        model = models.Submission
        exclude = ('parent',)


class DataSetSerializer (CachedSerializer, serializers.HyperlinkedModelSerializer):
    url = DataSetIdentityField()
    owner = UserRelatedField()
    keys = DataSetKeysRelatedField(source='*')

    places = DataSetPlaceSetSummarySerializer(source='*', read_only=True)
    submission_sets = DataSetSubmissionSetSummarySerializer(source='*', read_only=True)

    class Meta:
        model = models.DataSet

    def to_native(self, obj):
        data = super(DataSetSerializer, self).to_native(obj)

        if hasattr(obj, 'distance'):
            data['distance'] = str(obj.distance)

        return data


###############################################################################
#
# Pagination Serializers
# ----------------------
#

class PaginationMetadataSerializer (serializers.Serializer):
    length = serializers.Field(source='paginator.count')
    next = pagination.NextPageField(source='*')
    previous = pagination.PreviousPageField(source='*')
    page = serializers.Field(source='number')


class PaginatedResultsSerializer (pagination.BasePaginationSerializer):
    metadata = PaginationMetadataSerializer(source='*')


class FeatureCollectionSerializer (PaginatedResultsSerializer):
    results_field = 'features'

    def to_native(self, obj):
        data = super(FeatureCollectionSerializer, self).to_native(obj)
        data['type'] = 'FeatureCollection'
        return data
