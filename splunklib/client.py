# Copyright 2011-2012 Splunk, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License"): you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

#
# The purpose of this module is to provide a friendlier domain interface to 
# various Splunk endpoints. The approach here is to leverage the binding
# layer to capture endpoint context and provide objects and methods that
# offer simplified access their corresponding endpoints. The design avoids
# caching resource state. From the perspective of this module, the 'policy'
# for caching resource state belongs in the application or a higher level
# framework, and its the purpose of this module to provide simplified
# access to that resource state.
#
# A side note, the objects below that provide helper methods for updating eg:
# Entity state, are written so that they may be used in a fluent style.
#

"""This module provides a client interface for the `Splunk REST API
<http://docs.splunk.com/Documentation/Splunk/latest/RESTAPI/RESTcontents>`_."""

# UNDONE: Add Collection.refresh and list caching
# UNDONE: Resolve conflict between Collection.delete and name of REST method
# UNDONE: Add Endpoint.delete
# UNDONE: Add Entity.remove

import datetime
import json

from time import sleep
from urllib import urlencode, quote

from splunklib.binding import Context, HTTPError, AuthenticationError
import splunklib.data as data
from splunklib.data import record

__all__ = [
    "connect",
    "NotSupportedError",
    "OperationError",
    "Service"
]

PATH_APPS = "apps/local/"
PATH_CAPABILITIES = "authorization/capabilities/"
PATH_CONF = "configs/conf-%s/"
PATH_CONFS = "properties/"
PATH_DEPLOYMENT_CLIENTS = "deployment/client/"
PATH_DEPLOYMENT_TENANTS = "deployment/tenants/"
PATH_DEPLOYMENT_SERVERS = "deployment/server/"
PATH_DEPLOYMENT_SERVERCLASSES = "deployment/serverclass/"
PATH_EVENT_TYPES = "saved/eventtypes/"
PATH_FIRED_ALERTS = "alerts/fired_alerts/"
PATH_INDEXES = "data/indexes/"
PATH_INPUTS = "data/inputs/"
PATH_JOBS = "search/jobs/"
PATH_LOGGER = "server/logger/"
PATH_MESSAGES = "messages/"
PATH_ROLES = "authentication/roles/"
PATH_SAVED_SEARCHES = "saved/searches/"
PATH_STANZA = "configs/conf-%s/%s" # (file, stanza)
PATH_USERS = "authentication/users/"

XNAMEF_ATOM = "{http://www.w3.org/2005/Atom}%s"
XNAME_ENTRY = XNAMEF_ATOM % "entry"
XNAME_CONTENT = XNAMEF_ATOM % "content"

MATCH_ENTRY_CONTENT = "%s/%s/*" % (XNAME_ENTRY, XNAME_CONTENT)

# Filter the given state content record according to the given arg list.
def _filter_content(content, *args):
    if len(args) > 0:
        return record((k, content[k]) for k in args)
    return record((k, v) for k, v in content.iteritems()
        if k not in ['eai:acl', 'eai:attributes', 'type'])

# Construct a resource path from the given base path + resource name
def _path(base, name):
    if not base.endswith('/'): base = base + '/'
    return base + name

# Returns a path to the resource corresponding to the given conf name
def _path_conf(conf):
    return PATH_CONF % conf

# Load an atom record from the body of the given response
def _load_atom(response, match=None):
    return data.load(response.body.read(), match)

# Load an array of atom entries from the body of the given response
def _load_atom_entries(response):
    entries = _load_atom(response).feed.get('entry', None)
    if entries is None: return None
    return entries if isinstance(entries, list) else [entries]

# Load the sid from the body of the given response
def _load_sid(response):
    return _load_atom(response).response.sid

# Parse the given atom entry record into a generic entity state record
def _parse_atom_entry(entry):
    title = entry.get('title', None)

    elink = entry.get('link', [])
    elink = elink if isinstance(elink, list) else [elink]
    links = record((link.rel, link.href) for link in elink)

    # Retrieve entity content values
    content = entry.get('content', {})

    # Host entry metadata
    metadata = _parse_atom_metadata(content)

    # Filter some of the noise out of the content record
    content = record((k, v) for k, v in content.iteritems()
        if k not in ['eai:acl', 'eai:attributes', 'type'])

    return record({
        'title': title,
        'links': links,
        'access': metadata.access,
        'fields': metadata.fields,
        'content': content
    })

# Parse the metadata fields out of the given atom entry content record
def _parse_atom_metadata(content):
    # Hoist access metadata
    access = content.get('eai:acl', None)

    # Hoist content metadata (and cleanup some naming)
    attributes = content.get('eai:attributes', {})
    fields = record({
        'required': attributes.get('requiredFields', []),
        'optional': attributes.get('optionalFields', []),
        'wildcard': attributes.get('wildcardFields', [])})

    return record({'access': access, 'fields': fields})

# kwargs: scheme, host, port, app, owner, username, password
def connect(**kwargs):
    """Establishes an authenticated connection to a Splunk :class:`Service`
    instance.

    :param `host`: The host name (the default is *localhost*).
    :param `port`: The port number (the default is *8089*).
    :param `scheme`: The scheme for accessing the service (the default is 
                     *https*).
    :param `owner`: The owner namespace (optional).
    :param `app`: The app context (optional).
    :param `token`: The current session token (optional). Session tokens can be 
                    shared across multiple service instances.
    :param `username`: The Splunk account username, which is used to 
                       authenticate the Splunk instance.
    :param `password`: The password, which is used to authenticate the Splunk 
                       instance.
    :return: An initialized :class:`Service` instance.
    """
    return Service(**kwargs).login()

class Service(Context):
    """This class represents a Splunk service instance at a given address 
    (host:port), accessed using the *http* or *https* protocol scheme.
    
    A :class:`Service` instance also captures an optional namespace context 
    consisting of an optional owner name (or "-" wildcard) and optional app name
    (or "-" wildcard). To access :class:`Service` members, the instance must
    be authenticated by presenting credentials using the :meth:`login` method,
    or by constructing the instance using the :func:`connect` function, which 
    both creates and authenticates the instance.

    :param `host`: The host name (the default is *localhost*).
    :param `port`: The port number (the default is *8089*).
    :param `scheme`: The scheme for accessing the service (the default is 
                     *https*).
    :param `owner`: The owner namespace (optional).
    :param `app`: The app context (optional).
    :param `token`: The current session token (optional). Session tokens can be 
                    shared across multiple service instances.
    :param `username`: The Splunk account username, which is used to 
                       authenticate the Splunk instance.
    :param `password`: The password, which is used to authenticate the Splunk 
                       instance.
    """
    def __init__(self, **kwargs):
        Context.__init__(self, **kwargs)

    @property
    def apps(self):
        """Returns a collection of Splunk applications."""
        return Collection(self, PATH_APPS, item=Application)

    @property
    def confs(self):
        """Returns a collection of Splunk configurations."""
        return Confs(self)

    @property
    def capabilities(self):
        """Returns a list of system capabilities."""
        response = self.get(PATH_CAPABILITIES)
        return _load_atom(response, MATCH_ENTRY_CONTENT).capabilities

    @property
    def deployment_clients(self):
        """All clients of this Splunk instance's deployment server."""
        return DeploymentCollection(self, PATH_DEPLOYMENT_CLIENTS, item=DeploymentClient)

    @property
    def deployment_servers(self):
        """All deployment servers this Splunk instance refers to."""
        return DeploymentServers(self)

    @property
    def deployment_server_classes(self):
        """Serverclasses maintained by this Splunk instance's deployment server."""
        return DeploymentServerClasses(self)

    @property
    def deployment_tenants(self):
        """Tenants of this deployment server."""
        return DeploymentCollection(self, PATH_DEPLOYMENT_TENANTS, item=DeploymentTenant)

    @property
    def event_types(self):
        """Returns a collection of saved event types."""
        return Collection(self, PATH_EVENT_TYPES)

    @property
    def fired_alerts(self):
        """Returns a collection of alerts that have been fired by the service.
        """
        return Collection(self, PATH_FIRED_ALERTS, item=AlertGroup)

    @property
    def indexes(self):
        """Returns a collection of indexes."""
        return Collection(self, PATH_INDEXES, item=Index)

    @property
    def info(self):
        """Returns information about the service."""
        response = self.get("server/info")
        return _filter_content(_load_atom(response, MATCH_ENTRY_CONTENT))

    @property
    def inputs(self):
        """Returns a collection of configured inputs."""
        return Inputs(self)

    @property
    def jobs(self):
        """Returns a collection of current search jobs."""
        return Jobs(self)

    @property
    def loggers(self):
        """Returns a collection of service logging categories and their status.
        """
        return Loggers(self)

    @property
    def messages(self):
        """Returns a collection of service messages."""
        return Collection(self, PATH_MESSAGES, item=Message)

    # kwargs: enable_lookups, reload_macros, parse_only, output_mode
    def parse(self, query, **kwargs):
        """Parses a search query and returns a semantic map of the search.

        :param `query`: The search query to parse.
        :param `kwargs`: Optional arguments to pass to the ``search/parser`` 
                         endpoint.
        :return: A semantic map of the parsed search query.
        """
        return self.get("search/parser", q=query, **kwargs)

    def restart(self):
        """Restarts the service. The service will be unavailable until it has
        successfully restarted.
        """
        return self.get("server/control/restart")

    @property
    def roles(self):
        """Returns a collection of user roles."""
        return Collection(self, PATH_ROLES)

    def search(self, query, **kwargs):
        return self.jobs.create(query, **kwargs)

    @property
    def saved_searches(self):
        """Returns a collection of saved searches."""
        return SavedSearches(self)

    @property
    def settings(self):
        """Returns configuration settings for the service."""
        return Settings(self)

    @property
    def users(self):
        """Returns a collection of users."""
        return Users(self)

class Endpoint(object):
    """Individual resources in the REST API.
    
    An Endpoint represents a URI, such as /services/saved/searches. It
    has provides the common functionality of Collection and Entity
    (essentially HTTP get and post methods).
    """
    def __init__(self, service, path):
        self.service = service
        self.path = path if path.endswith('/') else path + '/'

    def get(self, relpath="", **kwargs):
        """Issues a ``GET`` request to a (possibly empty) path relative to this endpoint.

        Keyword arguments are passed as keyword arguments. For
        example, ``obj.get(arg1="abc", arg2="def")``.

        :param `relpath`: A path relative to the endpoint (optional).
        :param `kwargs`: Query arguments (optional).
        """
        return self.service.get("%s%s" % (self.path, relpath), **kwargs)

    def post(self, relpath="", **kwargs):
        """Issues a ``POST`` request to a (possibly empty) path relative to this endpoint.

        Keyword arguments are passed as fields in the POST body.
        
        :param `relpath`: A path relative to the endpoint (optional).
        :param `kwargs`: Form arguments (optional).
        """
        return self.service.post("%s%s" % (self.path, relpath), **kwargs)

# kwargs: path, app, owner, sharing, state
class Entity(Endpoint):
    """Base class for entities in the REST API such as saved searches or applications.

    Entity provides the majority of functionality required by entities
    in the REST API. Subclasses only implement the special cases for
    individual Entities, such as nicely making whitelists and
    blacklists in deployment serverclasses into Python lists.

    An Entity is addressed like a dictionary, with a few extensions,
    so the following all work::

        ent['email.action']
        ent['disabled']
        ent['whitelist']

    Many endpoints have values that share a prefix, such as
    ``email.to``, ``email.action``, ``email.subject``. You can extract
    the whole fields, or use the key ``email`` to get a dictionary of
    all the subelements. That is ``ent['email']`` will return a
    dictionary with the keys ``to``, ``action``, ``subject``, etc. If
    there are multiple levels of dots, then each level is made into a
    subdictionary, so ``email.body.salutation`` would be accessed at
    ``ent['email']['body']['salutation']`` or
    ``ent['email.body.salutation']``.
    """
    # Not every endpoint in the API is an Entity or a Collection. For
    # example, a saved search at saved/searches/{name} has an additional
    # method saved/searches/{name}/scheduled_times, but this isn't an
    # entity in its own right. In these cases, subclasses should
    # implement a method that uses the get and post methods inherited
    # from Endpoint, calls the _load_atom function (it's elsewhere in
    # client.py, but not a method of any object) to read the
    # information, and returns the extracted data in a Pythonesque form.
    #
    # The primary use of subclasses of Entity is to handle specially
    # named fields in the Entity. If you only need to provide a default
    # value for an optional field, subclass Entity and define a
    # dictionary ``defaults``. For instance,::
    #
    #     class Hypothetical(Entity):
    #         defaults = {'anOptionalField': 'foo',
    #                     'anotherField': 'bar'}
    #
    # If you have to do more than provide a default, such as rename or
    # actually process values, then define a new method with the
    # ``@property`` decorator.
    #
    #     class Hypothetical(Entity):
    #         @property
    #         def foobar(self):
    #             return self.content['foo'] + "-" + self.content["bar"]

    # Subclasses can override defaults the default values for
    # optional fields. See above.
    defaults = {} 

    def __init__(self, service, path, **kwargs):
        Endpoint.__init__(self, service, path)
        self._state = None
        self.refresh(kwargs.get('state', None)) # "Prefresh"

    def __call__(self, *args):
        return self.content(*args)

    # _lookup, __getattr__, and __getitem__ are arranged to make
    # access via a[b] or a.b work, but they're brittle. Be careful
    # when modifying them.
    #
    # The strategy for getting fields is:
    #   1. Look for a method or field of an object. This has to come
    #      first so that you can write custom methods to modify the
    #      behavior or names of fields.
    #   2. Look in self.state.content for any matches. Since
    #      self.state.content is a Record (see data.py for Record's
    #      definition) it handles items with separators in them
    #      properly.
    #   3. If there is no value there, turn to the class's defaults
    #      dictionary.
    # 
    # Both __getattr__ and __getitem__ dispatch to the same behavior
    # in _lookup.
    def _lookup(self, key):
        # Stage 1.
        try:
            # We already overrode __getattr__, so we have to go up to
            # the superclass's implementation or end up on infinite
            # recursion.
            return Endpoint.__getattr__(self, key)
        except AttributeError, ae:
            # Stage 2.
            try:
                # Despite its fancy dispatch, Record throws KeyErrors
                # sensibly.
                return self.content[key]
            except KeyError:
                # Stage 3.
                try:
                    return self.defaults[key]
                except KeyError:
                    raise KeyError("No such attribute %s" % key)

    def __getattr__(self, key):
        return self._lookup(key)

    def __getitem__(self, key):
        return self._lookup(key)

    # Load the Atom entry record from the given response - this is a method
    # because the "entry" record varies slightly by entity and this allows
    # for a subclass to override and handle any special cases.
    def _load_atom_entry(self, response):
        return _load_atom(response, XNAME_ENTRY).entry

    # Load the entity state record from the given response
    def _load_state(self, response):
        entry = self._load_atom_entry(response)
        return _parse_atom_entry(entry)

    def _run_method(self, relpath, **kwargs):
        """Run a method and return the content Record from the returned XML.

        A method is a relative path from an Entity that is not itself
        an Entity. _run_method assumes that the returned XML is an
        Atom field containing one Entry, and the contents of Entry is
        what should be the return value. This is right in enough cases
        to make this method useful.
        """
        response = self.get(relpath, **kwargs)
        data = self._load_atom_entry(response)
        rec = _parse_atom_entry(data)
        return rec.content

    def refresh(self, state=None):
        """Refreshes the cached state of this entity, using either the given
        state record, or by calling :meth:`read` if no state record is provided.
        """
        self._state = state if state is not None else self.read()
        return self

    @property
    def access(self):
        """Returns entity access metadata."""
        return self.state.access

    @property
    def content(self):
        """Returns the contents of the entity."""
        return self.state.content

    def disable(self):
        """Disables the entity at this endpoint."""
        self.post("disable")
        return self

    def enable(self):
        """Enables the entity at this endpoint."""
        self.post("enable")
        return self

    @property
    def fields(self):
        """Returns entity content metadata."""
        return self.state.fields

    @property
    def links(self):
        """Returns a dictionary of related resources."""
        return self.state.links

    @property
    def name(self):
        """Returns the entity name."""
        return self.state.title

    def read(self):
        """Reads the current state of the entity from the server."""
        return self._load_state(self.get())

    def reload(self):
        """Reloads the entity."""
        self.post("_reload")
        return self

    @property
    def state(self):
        """Returns the entity's state record."""
        if self._state is None: self.refresh()
        return self._state

    def update(self, **kwargs):
        """Updates the entity with the arguments you provide.

        Note that you cannot update the ``name`` field of an Entity,
        due to a peculiarity of the REST API.

        Many of the fields in the REST API are not valid Python
        identifiers, which means you cannot pass them as keyword
        arguments. That is, Python will fail to parse the following::

            x.update(check-new=False, email.to='boris@utopia.net')

        However, you can always explicitly use a dictionary to pass
        such keys::

            x.update(**{'check-new': False, 'email.to': 'boris@utopia.net'})
        """
        # The peculiarity in question: the REST API creates a new
        # Entity if we pass name in the dictionary, instead of the
        # expected behavior of updating this Entity. Therefore we
        # check for 'name' in kwargs and throw an error if it is
        # there.
        if 'name' in kwargs:
            raise ValueError("Cannot update the name of an Entity via the REST API.")
        self.post(**kwargs)
        return self

class Collection(Endpoint):
    """This class contains a collection of entities."""
    def __init__(self, service, path, item=Entity):
        Endpoint.__init__(self, service, path)
        self.item = item # Item accessor

    def __call__(self, **kwargs):
        return self.list(**kwargs)

    def __contains__(self, name):
        return self.contains(name)

    def __getitem__(self, key):
        for item in self.list():
            if item.name == key: return item
        raise KeyError, key

    def __delitem__(self, key):
        """Support for ``del ``*self*``[``*key``]``.

        :param key: The name of the entity to delete.
        :type key: string

        :rtype: Does not return anything, since del on a normal
                dictionary does not, and the del operator in Python
                cannot be used as an expression.

        This method is implemented for consistency with the interface
        of Python dictionaries.

        If there is no entity named *key* on the server, then throws a
        ``KeyError`` (again in analogy to ``dict``). This function
        always makes a roundtrip to the server.

        **Example**::

            import splunklib.client as client
            c = client.connect(...)
            saved_searches = c.saved_searches
            saved_searches.create('my_saved_search', 
                                  'search * | head 1')
            assert 'my_saved_search' in saved_searches
            del saved_searches['my_saved_search']
            assert 'my_saved_search' not in saved_searches
        """
        # If you update the documentation here, be sure to update it
        # on delete as well.
        self.delete(key)

    def __iter__(self):
        """Iterate over the names of entities in the collection.

        Implemented to give Collection a dict-like interface. Earlier
        versions iterated over the values; that has been changed to
        for consistency with ``dict``.

        This function always makes a roundtrip to the server.

        :rtype: iterator over strings.

        **Example**::

            import splunklib.client as client
            c = client.connect(...)
            saved_searches = c.saved_searches
            for name in saved_searches:
                print "Saved search named %s" % name
        """
        # If you update the documentation here, be sure to update it
        # on iterkeys as well.
        for item in self.list(): 
            yield item.name

    def __len__(self):
        """Enable ``len(...)`` for ``Collection``s.

        Implemented for consistency with a dict-like interface. No
        further failure modes beyond those possible for any method on
        an Endpoint.

        This function always makes a roundtrip to the server.

        **Example**::

            import splunklib.client as client
            c = client.connect(...)
            saved_searches = c.saved_searches
            n = len(saved_searches)
        """
        return len(self.list())

    def _load_list(self, response):
        """Loads an entity list from a response."""
        entries = _load_atom_entries(response)
        if entries is None: return []
        entities = []
        for entry in entries:
            state = _parse_atom_entry(entry)
            entity = self.item(
                self.service, 
                state.links.alternate,
                state=state)
            entities.append(entity)
        return entities

    def contains(self, name):
        """Indicates whether an entity name exists in the collection.
        
        :param `name`: The entity name.
        """
        for item in self.list():
            if item.name == name: return True
        return False

    def create(self, name, **kwargs):
        """Creates an entity in this collection.

        :param `name`: The name of the entity to create.
        :param `kwargs`: Additional entity-specific arguments (optional).
        :return: The new entity.
        """
        if not isinstance(name, basestring): 
            raise ValueError("Invalid argument: 'name'")
        self.post(name=name, **kwargs)
        return self[name] # UNDONE: Extra round-trip to retrieve entity

    def delete(self, name):
        """Delete the entity *name* from the collection.

        :param name: The name of the entity to delete.
        :type name: string
        :rtype: the collection ``self``.

        This method is implemented for consistency with the REST
        interface's DELETE method.

        If there is no entity named *name* on the server, then throws
        a ``KeyError``. This function always makes a roundtrip to the
        server.

        **Example**::

            import splunklib.client as client
            c = client.connect(...)
            saved_searches = c.saved_searches
            saved_searches.create('my_saved_search', 
                                  'search * | head 1')
            assert 'my_saved_search' in saved_searches
            saved_searches.delete('my_saved_search')
            assert 'my_saved_search' not in saved_searches
        """
        # If you update the documentation here, be sure you do so on
        # __delitem__ as well.
        
        try:
            self.service.delete(_path(self.path, name))
        except HTTPError as he:
            # An HTTPError with status code 404 means that the entity
            # has already been deleted, and we reraise it as a
            # KeyError.
            if he.status == 404:
                raise KeyError("No such entity %s" % name)
            else:
                raise
        return self

    def itemmeta(self):
        """Returns metadata for members of the collection."""
        response = self.get("_new")
        content = _load_atom(response, MATCH_ENTRY_CONTENT)
        return _parse_atom_metadata(content)

    def iteritems(self):
        """Iterate over the (name,entity) pairs in this collection.

        :rtype: iterator over tuples of ``(``*string*``, ``*Entity*``)``.

        Implemented to give Collection a dict-like interface. This
        function always makes a roundtrip to the server.

        **Example**::

            import splunklib.client as client
            c = client.connect(...)
            saved_searches = c.saved_searches
            for name,entity in saved_searches:
                assert entity.name == name
        """
        for item in self.list():
            yield (item.name, item)

    def iterkeys(self):
        """Iterate over the names of entities in this collection.

        :rtype: iterator over strings.

        Implemented to give Collection a dict-like interface. This
        function always makes a roundtrip to the server.

        **Example**::

            import splunklib.client as client
            c = client.connect(...)
            saved_searches = c.saved_searches
            for name in saved_searches:
                print "Saved search named %s" % name
        """
        # If you update the documentation here, be sure to update it
        # on __iter__ as well.
        return iter(self)

    def itervalues(self):
        """Iterate over the entities in this collection.

        :rtype: iterator over ``Entity``s.

        Implemented to give Collection a dict-like interface. This
        function always makes a roundtrip to the server.

        **Example**::

            import splunklib.client as client
            c = client.connect(...)
            saved_searches = c.saved_searches
            for entity in saved_searches.itervalues():
                print "Saved search named %s" % entity.name
        """
        for item in self.list(): 
            yield item

    def keys(self):
        """Return a list of the names of entities in this collection.

        :rtype: list of strings.

        Implemented to give Collection a dict-like interface. This
        function always makes a roundtrip to the server.
        
        **Example**::

            import splunklib.client as client
            c = client.connect(...)
            saved_searches = c.saved_searches
            for name in saved_searches.keys():
                print "Saved search named %s" % name
        """
        return list(self.iterkeys())

    # kwargs: count, offset, search, sort_dir, sort_key, sort_mode
    def list(self, count=-1, **kwargs):
        """Returns the contents of the collection.

        :param `count`: The maximum number of items to return (optional).
        :param `offset`: The offset of the first item to return (optional).
        :param `search`: The search expression to filter responses (optional).
        :param `sort_dir`: The direction to sort returned items: *asc* or *desc*
                           (optional).
        :param `sort_key`: The field to use for sorting (optional).
        :param `sort_mode`: The collating sequence for sorting returned items:
                            *auto*, *alpha*, *alpha_case*, *num* (optional).

        This function always makes a roundtrip to the server, and
        makes not attempt at caching.
        """
        response = self.get(count=count, **kwargs)
        return self._load_list(response)

class Conf(Collection):
    """This class contains a single configuration, which is a collection of 
    stanzas."""
    def __init__(self, service, name):
        self.name = name
        Collection.__init__(self, service, _path_conf(name), item=Stanza)

class Confs(Collection):
    """This class contains a collection of configurations."""
    def __init__(self, service):
        Collection.__init__(self, service, PATH_CONFS, 
            item=lambda service, path, **kwargs:
                Conf(service, kwargs['state'].title))

    def create(self, name, **kwargs):
        if not isinstance(name, basestring): 
            raise ValueError("Invalid argument: 'name'")
        self.post(__conf=name, **kwargs)
        return self[name] # UNDONE: Extra round-trip to retrieve entity

class Stanza(Entity):
    """This class contains a single configuration stanza."""
    def submit(self, stanza):
        """Populates a stanza in the .conf file."""
        message = { 'method': "POST", 'body': stanza }
        self.service.request(self.path, message)
        return self

class AlertGroup(Entity):
    """This class contains an entity that represents a group of fired alerts 
    that can be accessed through the :meth:`alerts` property."""
    def __init__(self, service, path, **kwargs):
        Entity.__init__(self, service, path, **kwargs)

    @property
    def alerts(self):
        """Returns a collection of triggered alert instances."""
        return Collection(self.service, self.path)

    @property
    def count(self):
        """Returns the count of triggered alerts."""
        return int(self.content.triggered_alert_count)

class Index(Entity):
    """This class is an index class used to access specific operations."""
    def __init__(self, service, path, **kwargs):
        Entity.__init__(self, service, path, **kwargs)

    def attach(self, host=None, source=None, sourcetype=None):
        """Opens a stream (a writable socket) for writing events to the index.

        :param `host`: The host value for events written to the stream.
        :param `source`: The source value for events written to the stream.
        :param `sourcetype`: The sourcetype value for events written to the 
                            stream.
        """
        args = { 'index': self.name }
        if host is not None: args['host'] = host
        if source is not None: args['source'] = source
        if sourcetype is not None: args['sourcetype'] = sourcetype
        path = "receivers/stream?%s" % urlencode(args)

        # Since we need to stream to the index connection, we have to keep
        # the connection open and use the Splunk extension headers to note
        # the input mode
        cn = self.service.connect()
        cn.write("POST %s HTTP/1.1\r\n" % self.service.fullpath(path))
        cn.write("Host: %s:%s\r\n" % (self.service.host, self.service.port))
        cn.write("Accept-Encoding: identity\r\n")
        cn.write("Authorization: %s\r\n" % self.service.token)
        cn.write("X-Splunk-Input-Mode: Streaming\r\n")
        cn.write("\r\n")
        return cn

    def clean(self, timeout=60):
        """Deletes the contents of the index.
        
        :param `timeout`: The time-out period for the operation, in seconds (the
                          default is 60).
        """
        saved = self.refresh()('maxTotalDataSizeMB', 'frozenTimePeriodInSecs')
        self.update(maxTotalDataSizeMB=1, frozenTimePeriodInSecs=1)
        self.roll_hot_buckets()

        # Wait until the event count goes to zero
        count = 0
        while self.content.totalEventCount != '0' and count < timeout:
            sleep(1)
            count += 1
            self.refresh()

        self.update(**saved) # Restore original values
        if self.content.totalEventCount != '0':
            raise OperationError, "Operation timed out."
        return self

    def roll_hot_buckets(self):
        """Performs rolling hot buckets for this index."""
        self.post("roll-hot-buckets")
        return self

    def submit(self, event, host=None, source=None, sourcetype=None):
        """Submits an event to the index using ``HTTP POST``.

        :param `host`: The host value of the event.
        :param `source`: The source value of the event.
        :param `sourcetype`: The sourcetype value of the event.
        """
        args = { 'index': self.name }
        if host is not None: args['host'] = host
        if source is not None: args['source'] = source
        if sourcetype is not None: args['sourcetype'] = sourcetype

        # The reason we use service.request directly rather than POST
        # is that we are not sending a POST request encoded using 
        # x-www-form-urlencoded (as we do not have a key=value body),
        # because we aren't really sending a "form".
        path = "receivers/simple?%s" % urlencode(args)
        message = { 'method': "POST", 'body': event }
        self.service.request(path, message)
        return self

    # kwargs: host, host_regex, host_segment, rename-source, sourcetype
    def upload(self, filename, **kwargs):
        """Uploads a file for immediate indexing. 
        
        .. Note: The file must be locally accessible from the server.
        
        :param `filename`: The name of the file to upload. The file can be 
                           a plain, compressed, or archived file.
        :param `kwargs`: Additional arguments (optional). For details, see the 
                         `POST data/inputs/oneshot 
                         <http://docs.splunk.com/Documentation/Splunk/4.2.4/RESTAPI/RESTinput#POST_data.2Finputs.2Foneshot>`_
                         endpoint in the Splunk REST API documentation.
        """
        kwargs['index'] = self.name
        path = 'data/inputs/oneshot'
        self.service.post(path, name=filename, **kwargs)
        return self

class Input(Entity):
    """This class represents a Splunk input. This class is the base for all 
    typed input classes and is also used when the client does not recognize an
    input kind."""
    def __init__(self, service, path, kind, **kwargs):
        Entity.__init__(self, service, path, **kwargs)
        self.kind = kind

# Directory of known input kinds that maps from input kind to path relative 
# to data/inputs, eg: inputs of kind 'splunktcp' map to a relative path
# of 'tcp/cooked' and therefore an endpoint path of 'data/inputs/tcp/cooked'.
INPUT_KINDMAP = {
    'ad': "ad",
    'monitor': "monitor",
    'registry': "registry",
    'script': "script",
    'tcp': "tcp/raw",
    'splunktcp': "tcp/cooked",
    'udp': "udp",
    'win-event-log-collections': "win-event-log-collections", 
    'win-perfmon': "win-perfmon",
    'win-wmi-collections': "win-wmi-collections"
}

# Inputs is a "kinded" collection, which is a heterogenous collection where
# each item is tagged with a kind, that provides a single merged view of all
# input kinds.
# UNDONE: contains needs to take a kind arg to disambiguate
class Inputs(Collection):
    """This class represents a collection of inputs. The collection is 
    heterogeneous and each member of the collection contains a *kind* property
    that indicates the specific type of input."""

    def __init__(self, service, kindmap=None):
        Collection.__init__(self, service, PATH_INPUTS)
        self._kindmap = kindmap if kindmap is not None else INPUT_KINDMAP
        
    # args: kind*
    def __call__(self, *args):
        return self.list(*args)

    def create(self, kind, name, **kwargs):
        """Creates an input of a specific kind in this collection, with any 
        arguments you specify. 

        :param `kind`: The kind of input to create.
        :param `name`: The input name.
        :param `kwargs`: Additional entity-specific arguments (optional). For
                         valid arguments, see the POST requests for the
                         `/data/inputs/ 
                         <http://docs.splunk.com/Documentation/Splunk/latest/RESTAPI/RESTinput>`_ 
                         endpoints in the Splunk REST API documentation.
        :return: The new input.
        """
        kindpath = self.kindpath(kind)
        self.service.post(kindpath, name=name, **kwargs)
        return Input(self.service, _path(kindpath, name), kind)

    def delete(self, name):
        """Removes an input from the collection.
        
        :param `name`: The name of the input to remove.
        """
        self.service.delete(self[name].path) # UNDONE: Should be item.remove()
        return self

    def itemmeta(self, kind):
        """Returns metadata for the members of a given kind."""
        response = self.get("%s/_new" % self._kindmap[kind])
        content = _load_atom(response, MATCH_ENTRY_CONTENT)
        return _parse_atom_metadata(content)

    @property
    def kinds(self):
        """Returns the list of input kinds that this collection may 
        contain."""
        return self._kindmap.keys()

    def kindpath(self, kind):
        """Returns a path to the resources for a given input kind.

        :param `kind`: The input kind.
        """
        return self.path + self._kindmap[kind]

    # args: kind*
    def list(self, *args):
        """Returns a list of inputs that belong to the collection. You can also
        filter by one or more input kinds.

        :param `args`: The input kinds to return (optional).
        """
        kinds = args if len(args) > 0 else self._kindmap.keys()

        entities = []
        for kind in kinds:
            response = None
            try:
                response = self.service.get(self.kindpath(kind), count=-1)
            except HTTPError as e:
                if e.status == 404: 
                    continue # No inputs of this kind
                else: 
                    raise
                
            # UNDONE: Should use _load_list for the following, but need to
            # pass kind to the `item` method.
            entries = _load_atom_entries(response)
            if entries is None: continue # No inputs to process
            for entry in entries:
                state = _parse_atom_entry(entry)
                path = state.links.alternate
                entity = Input(self.service, path, kind, state=state)
                entities.append(entity)

        return entities

    def oneshot(self, **kwargs):
        pass

class Job(Entity): 
    """This class represents a search job."""
    def __init__(self, service, path, **kwargs):
        Entity.__init__(self, service, path, **kwargs)

    # The Job entry record is returned at the root of the response
    def _load_atom_entry(self, response):
        return _load_atom(response).entry

    def cancel(self):
        """Stops the current search and deletes the result cache."""
        self.post("control", action="cancel")
        return self

    def disable_preview(self):
        """Disables preview for this job."""
        self.post("control", action="disablepreview")
        return self

    def events(self, **kwargs):
        """Returns an InputStream IO handle for this job's events."""
        return self.get("events", **kwargs).body

    def enable_preview(self):
        """Enables preview for this job (although doing so might slow search 
        considerably)."""
        self.post("control", action="enablepreview")
        return self

    def finalize(self):
        """Stops the job and provides intermediate results available for 
        retrieval."""
        self.post("control", action="finalize")
        return self

    @property
    def name(self):
        """Returns the name of the search job."""
        return self.sid

    def pause(self):
        """Suspends the current search."""
        self.post("control", action="pause")
        return self

    def preview(self, **kwargs):
        """Returns the InputStream IO handle to the preview results for this 
        job.

        :param `kwargs`: Additional preview arguments (optional). For details, 
                         see the `GET search/jobs/{search_id}/results_preview 
                         <http://docs.splunk.com/Documentation/Splunk/4.2.4/RESTAPI/RESTsearch#GET_search.2Fjobs.2F.7Bsearch_id.7D.2Fresults_preview>`_ 
                         endpoint in the REST API documentation.
        """
        return self.get("results_preview", **kwargs).body

    def read(self):
        """Returns the job's current state record, corresponding to the
        current state of the server-side resource."""
        # If the search job is newly created, it is possible that we will 
        # get 204s (No Content) until the job is ready to respond.
        count = 0
        while count < 10:
            response = self.get()
            if response.status == 204: 
                sleep(1) 
                count += 1
                continue
            return self._load_state(response)
        raise OperationError, "Operation timed out."

    def results(self, **kwargs):
        """Returns an InputStream IO handle to the search results for this job.

        :param `kwargs`: Additional results arguments (optional). For details, 
                         see the `GET search/jobs/{search_id}/results 
                         <http://docs.splunk.com/Documentation/Splunk/4.2.4/RESTAPI/RESTsearch#GET_search.2Fjobs.2F.7Bsearch_id.7D.2Fresults>`_ 
                         endpoint in the REST API documentation.
        """
        return self.get("results", **kwargs).body

    def searchlog(self, **kwargs):
        """Returns an InputStream IO handle to the search log for this job.

        :param `kwargs`: Additional search log arguments (optional). For 
                         details, see the 
                         `GET search/jobs/{search_id}/search.log 
                         <http://docs.splunk.com/Documentation/Splunk/4.2.4/RESTAPI/RESTsearch#GET_search.2Fjobs.2F.7Bsearch_id.7D.2Fsearch.log>`_ 
                         endpoint in the REST API documentation.
        """
        return self.get("search.log", **kwargs).body

    def set_priority(self, value):
        """Sets this job's search priority in the range of 0-10.

        :param `value`: The search priority.
        """
        self.post('control', action="setpriority", priority=value)
        return self

    @property
    def sid(self):
        """Returns this job's search ID (sid)."""
        return self.content.get('sid', None)

    def summary(self, **kwargs):
        """Returns an InputStream IO handle to the job's summary.
        
        :param `kwargs`: Additional summary arguments (optional). For details, 
                         see the `GET search/jobs/{search_id}/summary 
                         <http://docs.splunk.com/Documentation/Splunk/4.2.4/RESTAPI/RESTsearch#GET_search.2Fjobs.2F.7Bsearch_id.7D.2Fsummary>`_ 
                         endpoint in the REST API documentation.
        """
        return self.get("summary", **kwargs).body

    def timeline(self, **kwargs):
        """Returns an InputStream IO handle to the job's timeline results.

        :param `kwargs`: Additional timeline arguments (optional). For details, 
                         see the `GET search/jobs/{search_id}/timeline 
                         <http://docs.splunk.com/Documentation/Splunk/4.2.4/RESTAPI/RESTsearch#GET_search.2Fjobs.2F.7Bsearch_id.7D.2Ftimeline>`_ 
                         endpoint in the REST API documentation.
        """
        return self.get("timeline", **kwargs).body

    def touch(self):
        """Extends the expiration time of the search to the current time plus 
        the time-to-live value (now + ttl)."""
        self.post("control", action="touch")
        return self

    def set_ttl(self, value):
        """Set the job's time-to-live (ttl) value, which is the time before the 
        search job expires and is still available.

        :param `value`: The ttl value, in seconds.
        """
        self.post("control", action="setttl", ttl=value)
        return self

    def unpause(self):
        """Resumes the current search, if paused."""
        self.post("control", action="unpause")
        return self

class Jobs(Collection):
    """This class represents a collection of search jobs."""
    def __init__(self, service):
        Collection.__init__(self, service, PATH_JOBS, item=Job)

    def create(self, query, **kwargs):
        if kwargs.get("exec_mode", None) == "oneshot":
            raise TypeError("Cannot specify exec_mode=oneshot; use the oneshot method instead.")
        response = self.post(search=query, **kwargs)
        sid = _load_sid(response)
        return Job(self.service, PATH_JOBS + sid)

    def oneshot(self, query, **kwargs):

        # We take advantage of the search results returning JSON
        # output (at least in recent versions). This is far easier
        # than dealing with the XML.
        if "exec_mode" in kwargs:
            raise TypeError("Cannot specify an exec_mode to oneshot.")
        response = self.post(search=query, exec_mode="oneshot", output_mode='json', **kwargs)
        return json.loads(response.body.read())

    def list(self, count=0, **kwargs):
        return Collection.list(self, count, **kwargs)

class Loggers(Collection):
    """This class represents a collection of service logging categories."""
    def __init__(self, service):
        Collection.__init__(self, service, PATH_LOGGER)

    def itemmeta(self):
        raise NotSupportedError

class Message(Entity):
    def __init__(self, service, path, **kwargs):
        Entity.__init__(self, service, path, **kwargs)

    @property
    def value(self):
        """Returns the message value."""
        return self[self.name]

class SavedSearch(Entity):
    """This class represents a saved search."""
    def __init__(self, service, path, **kwargs):
        Entity.__init__(self, service, path, **kwargs)

    def acknowledge(self):
        """Acknowledges the suppression of alerts from this saved search and 
        resumes alerting."""
        self.post("acknowledge")
        return self

    def dispatch(self, **kwargs):
        """Runs the saved search and returns the resulting search job.

        :param `kwargs`: Additional dispatch arguments (optional). For details, 
                         see the `POST saved/searches/{name}/dispatch
                         <http://docs.splunk.com/Documentation/Splunk/4.2.4/RESTAPI/RESTsearch#POST_saved.2Fsearches.2F.7Bname.7D.2Fdispatch>`_ 
                         endpoint in the REST API documentation.
        :return: The new search job.
        """
        response = self.post("dispatch", **kwargs)
        sid = _load_sid(response)
        return Job(self.service, PATH_JOBS + sid)

    def history(self):
        """Returns a list of search jobs corresponding to this saved search.

        :return: A list of :class:`Job` objects.
        """
        response = self.get("history")
        entries = _load_atom_entries(response)
        if entries is None: return []
        jobs = []
        for entry in entries:
            job = Job(self.service, PATH_JOBS + entry.title)
            jobs.append(job)
        return jobs

    def update(self, search=None, **kwargs):
        """Updates the saved search with any additional arguments.

        :param `search`: The search string of this saved search (optional).
        :param `kwargs`: Additional update arguments (optional). 
        """
        # Updates to a saved search *require* that the search string be 
        # passed, so we pass the current search string if a value wasn't
        # provided by the caller.
        if search is None: search = self.content.search
        Entity.update(self, search=search, **kwargs)
        return self

    def scheduled_times(self, earliest_time='now', latest_time='+1h'):
        """Returns the times when this search is scheduled to run.

        By default it returns the times in the next hour. For other
        periods, set *earliest_time* and *latest_time*. For example,
        for all times in the last day use ``earliest_time=-1d`` and
        ``latest_time=now``.
        """
        response = self.get("scheduled_times", 
                            earliest_time=earliest_time, 
                            latest_time=latest_time)
        data = self._load_atom_entry(response)
        rec = _parse_atom_entry(data)
        times = [datetime.datetime.fromtimestamp(int(t))
                 for t in rec.content.scheduled_times]
        return times

    def suppress(self, expiration):
        """Skip any scheduled runs of this search in the next *expiration* seconds."""
        self.post("suppress", suppressed="1", expiration=expiration)
        return self

    @property
    def suppressed(self):
        """The number of seconds that this search is blocked from running (possibly 0)."""
        r = self._run_method("suppress")
        if r.suppressed == "1":
            return int(r.expiration)
        else:
            return 0

    def unsuppress(self):
        """Cancel suppression and make this search run as scheduled."""
        self.post("suppress", suppressed="0", expiration="0")
        return self
        

class SavedSearches(Collection):
    """This class represents a collection of saved searches."""
    def __init__(self, service):
        Collection.__init__(
            self, service, PATH_SAVED_SEARCHES, item=SavedSearch)

    def create(self, name, search, **kwargs):
        return Collection.create(self, name, search=search, **kwargs)

class Settings(Entity):
    """This class represents configuration settings for a Splunk service."""
    def __init__(self, service, **kwargs):
        Entity.__init__(self, service, "server/settings", **kwargs)

    # Updates on the settings endpoint are POSTed to server/settings/settings.
    def update(self, **kwargs):
        self.service.post("server/settings/settings", **kwargs)
        return self

# Splunk automatically lowercases new user names so we need to match that 
# behavior here to ensure that the subsequent member lookup works correctly.
class Users(Collection):
    """This class represents a Splunk user."""
    def __init__(self, service):
        Collection.__init__(self, service, PATH_USERS)

    def __getitem__(self, key):
        return Collection.__getitem__(self, key.lower())

    def contains(self, name):
        return Collection.contains(self, name.lower())

    def create(self, name, **kwargs):
        return Collection.create(self, name.lower(), **kwargs)

    def delete(self, name):
        return Collection.delete(self, name.lower())

class OperationError(Exception): 
    """Raised for a failed operation, such as a time out."""
    pass

class NotSupportedError(Exception): 
    """Raised for operations that are not supported on a given object."""
    pass

class DeploymentCollection(Collection):
    def __init__(self, service, path, item):
        Collection.__init__(self, service, path, item=item)

    def create(self, name, **kwargs):
        raise NotSupportedError("Cannot create %s with the REST API." % self.__class__.__name__)

    def delete(self, name):
        raise NotSupportedError("Cannot delete %s with the REST API." % self.__class__.__name__)

    def list(self, count=0, **kwargs):
        return Collection.list(self, count=count, **kwargs)

class DeploymentTenant(Entity):
    """Binding for /deployments/tenants/{name}."""
    @property
    def check_new(self):
        """Will the server inform clients of updated configuration?"""
        return self.state.content.get('check-new', False)

    def update(self, **kwargs):
        if 'check_new' in kwargs:
            kwargs['check-new'] = kwargs.pop('check_new')
        self.service.post(PATH_DEPLOYMENT_TENANTS + self.name, **kwargs)
        return self

class DeploymentServerClass(Entity):
    """Represents a deployment server class.

    Binds /deployments/serverclass/{name}.
    """
    defaults = {'endpoint': None, 
                'tmpfolder': None,
                'filterType': None,
                'targetRepositoryLocation': None,
                'repositoryLocation': None,
                'continueMatching': None}

    @property
    def blacklist(self):
        if 'blacklist' in self.content:
            return self.content.blacklist.split(',')
        else:
            return None

    def delete(self, **kwargs):
        raise NotSupportedError("Cannot delete server classes via the REST API")

    @property
    def whitelist(self):
        if 'whitelist' in self.content:
            return self.content.whitelist.split(',')
        else:
            return None

class DeploymentServerClasses(DeploymentCollection):
    """Binding for /deployment/serverclasses"""
    def __init__(self, service):
        Collection.__init__(self, service, PATH_DEPLOYMENT_SERVERCLASSES, item=DeploymentServerClass)

    def create(self, name, **kwargs):
        if 'blacklist' in kwargs:
            for i,v in enumerate(kwargs['blacklist']):
                kwargs['blacklist.%d' % i] = v
            kwargs.pop('blacklist')
        if 'whitelist' in kwargs:
            for i,v in enumerate(kwargs['whitelist']):
                kwargs['whitelist.%d' % i] = v
            kwargs.pop('whitelist')
        if not 'filterType' in kwargs:
            kwargs['filterType'] = 'blacklist'
        return Collection.create(self, name, **kwargs)

class DeploymentServer(Entity):
    """Binding for /deployment/server/{name}"""
    @property
    def whitelist(self):
        return self.content.get('whitelist.0', None)

    @property
    def check_new(self):
        return self.content.get('check-new', False)

    def update(self, **kwargs):
        if 'disabled' in kwargs:
            kwargs['disabled'] = '1' if kwargs['disabled'] else '0'
        if 'check_new' in kwargs:
            kwargs['check-new'] = kwargs.pop('check_new')
        if 'whitelist' in kwargs:
            kwargs['whitelist.0'] = kwargs.pop('whitelist')
        self.service.post(PATH_DEPLOYMENT_SERVERS + self.name, **kwargs)
        return self

class DeploymentServers(DeploymentCollection):
    """Binding for /deployment/server"""
    def __init__(self, service):
        Collection.__init__(self, service, PATH_DEPLOYMENT_SERVERS, item=DeploymentServer)

    # Override this because Collection defaults to count=-1
    def list(self, count=0, **kwargs):
        return Collection.list(self, count=count, **kwargs)

    def create(self, name, **kwargs):
        raise NotSupportedError("Cannot create deployment servers with the REST API.")

class DeploymentClient(Entity):
    """Binding for /deployment/client/{name}"""
    @property
    def serverClasses(self):
        if 'serverClasses' in self.content:
            return self.content['serverClasses'].split(',')
        else:
            return []

class Application(Entity):
    """Binding for /apps/local/{name}."""
    @property
    def setupInfo(self):
        return self.content.get('eai:setup', None)

    def package(self):
        return self._run_method("package")

    def updateInfo(self):
        return self._run_method("update")

    


