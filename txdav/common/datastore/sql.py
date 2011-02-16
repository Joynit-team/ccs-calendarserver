# -*- test-case-name: txdav.caldav.datastore.test.test_sql,txdav.carddav.datastore.test.test_sql -*-
##
# Copyright (c) 2010-2011 Apple Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
##

"""
SQL data store.
"""

__all__ = [
    "CommonDataStore",
    "CommonStoreTransaction",
    "CommonHome",
]


from zope.interface import implements, directlyProvides

from twext.python.log import Logger, LoggingMixIn
from twext.web2.dav.element.rfc2518 import ResourceType
from twext.web2.dav.element.parser import WebDAVDocument
from twext.web2.http_headers import MimeType

from twisted.python import hashlib
from twisted.python.modules import getModule
from twisted.python.util import FancyEqMixin

from twisted.internet.defer import inlineCallbacks, returnValue, succeed

from twisted.application.service import Service

from twext.internet.decorate import memoizedKey

from txdav.common.datastore.sql_legacy import PostgresLegacyNotificationsEmulator
from txdav.caldav.icalendarstore import ICalendarTransaction, ICalendarStore

from txdav.carddav.iaddressbookstore import IAddressBookTransaction

from txdav.common.datastore.sql_tables import schema
from txdav.common.datastore.sql_tables import NOTIFICATION_HOME_TABLE, _BIND_MODE_OWN, \
    _BIND_STATUS_ACCEPTED, NOTIFICATION_OBJECT_REVISIONS_TABLE
from txdav.common.icommondatastore import HomeChildNameNotAllowedError, \
    HomeChildNameAlreadyExistsError, NoSuchHomeChildError, \
    ObjectResourceNameNotAllowedError, ObjectResourceNameAlreadyExistsError, \
    NoSuchObjectResourceError
from txdav.common.inotifications import INotificationCollection, \
    INotificationObject

from twext.enterprise.dal.syntax import Parameter
from twext.python.clsprop import classproperty
from twext.enterprise.dal.syntax import Select
from twext.enterprise.dal.syntax import Lock
from twext.enterprise.dal.syntax import Insert
from twext.enterprise.dal.syntax import Max
from twext.enterprise.dal.syntax import default
from twext.enterprise.dal.syntax import Delete
from twext.enterprise.dal.syntax import Update

from txdav.base.propertystore.base import PropertyName
from txdav.base.propertystore.none import PropertyStore as NonePropertyStore
from txdav.base.propertystore.sql import PropertyStore

from twistedcaldav.customxml import NotificationType
from twistedcaldav.dateops import datetimeMktime, parseSQLTimestamp


v1_schema = getModule(__name__).filePath.sibling("sql_schema_v1.sql").getContent()

log = Logger()

ECALENDARTYPE = 0
EADDRESSBOOKTYPE = 1

# Labels used to identify the class of resource being modified, so that
# notification systems can target the correct application
NotifierPrefixes = {
    ECALENDARTYPE : "CalDAV",
    EADDRESSBOOKTYPE : "CardDAV",
}

class CommonDataStore(Service, object):

    implements(ICalendarStore)

    def __init__(self, sqlTxnFactory, notifierFactory, attachmentsPath,
                 enableCalendars=True, enableAddressBooks=True,
                 label="unlabeled"):
        assert enableCalendars or enableAddressBooks

        self.sqlTxnFactory = sqlTxnFactory
        self.notifierFactory = notifierFactory
        self.attachmentsPath = attachmentsPath
        self.enableCalendars = enableCalendars
        self.enableAddressBooks = enableAddressBooks
        self.label = label


    def eachCalendarHome(self):
        """
        @see L{ICalendarStore.eachCalendarHome}
        """
        return []


    def eachAddressbookHome(self):
        """
        @see L{IAddressbookStore.eachAddressbookHome}
        """
        return []



    def newTransaction(self, label="unlabeled", migrating=False):
        """
        @see L{IDataStore.newTransaction}
        """
        return CommonStoreTransaction(
            self,
            self.sqlTxnFactory(),
            self.enableCalendars,
            self.enableAddressBooks,
            self.notifierFactory,
            label,
            migrating,
        )



class CommonStoreTransaction(object):
    """
    Transaction implementation for SQL database.
    """
    _homeClass = {}

    id = 0

    def __init__(self, store, sqlTxn,
                 enableCalendars, enableAddressBooks,
                 notifierFactory, label, migrating=False):
        self._store = store
        self._calendarHomes = {}
        self._addressbookHomes = {}
        self._notificationHomes = {}
        self._postCommitOperations = []
        self._notifierFactory = notifierFactory
        self._label = label
        self._migrating = migrating
        self._primaryHomeType = None

        CommonStoreTransaction.id += 1
        self._txid = CommonStoreTransaction.id

        extraInterfaces = []
        if enableCalendars:
            extraInterfaces.append(ICalendarTransaction)
            self._primaryHomeType = ECALENDARTYPE
        if enableAddressBooks:
            extraInterfaces.append(IAddressBookTransaction)
            if self._primaryHomeType is None:
                self._primaryHomeType = EADDRESSBOOKTYPE
        directlyProvides(self, *extraInterfaces)

        from txdav.caldav.datastore.sql import CalendarHome
        from txdav.carddav.datastore.sql import AddressBookHome
        CommonStoreTransaction._homeClass[ECALENDARTYPE] = CalendarHome
        CommonStoreTransaction._homeClass[EADDRESSBOOKTYPE] = AddressBookHome
        self._sqlTxn = sqlTxn
        self.paramstyle = sqlTxn.paramstyle


    def store(self):
        return self._store


    def __repr__(self):
        return 'PG-TXN<%s>' % (self._label,)


    @memoizedKey('uid', '_calendarHomes')
    def calendarHomeWithUID(self, uid, create=False):
        return self.homeWithUID(ECALENDARTYPE, uid, create=create)


    @memoizedKey("uid", "_addressbookHomes")
    def addressbookHomeWithUID(self, uid, create=False):
        return self.homeWithUID(EADDRESSBOOKTYPE, uid, create=create)


    def homeWithUID(self, storeType, uid, create=False):
        if storeType not in (ECALENDARTYPE, EADDRESSBOOKTYPE):
            raise RuntimeError("Unknown home type.")

        return self._homeClass[storeType].homeWithUID(self, uid, create)

    @inlineCallbacks
    def calendarHomeWithResourceID(self, rid):
        uid = (yield self._homeClass[ECALENDARTYPE].homeUIDWithResourceID(self, rid))
        if uid:
            result = (yield self.calendarHomeWithUID(uid))
        else:
            result = None
        returnValue(result)

    @inlineCallbacks
    def addressbookHomeWithResourceID(self, rid):
        uid = (yield self._homeClass[EADDRESSBOOKTYPE].homeUIDWithResourceID(self, rid))
        if uid:
            result = (yield self.addressbookHomeWithUID(uid))
        else:
            result = None
        returnValue(result)

    @memoizedKey("uid", "_notificationHomes")
    def notificationsWithUID(self, uid):
        """
        Implement notificationsWithUID.
        """
        return NotificationCollection.notificationsWithUID(self, uid)


    def postCommit(self, operation):
        """
        Run things after C{commit}.
        """
        self._postCommitOperations.append(operation)


    def execSQL(self, *a, **kw):
        """
        Execute some SQL (delegate to L{IAsyncTransaction}).
        """
        return self._sqlTxn.execSQL(*a, **kw)


    def commit(self):
        """
        Commit the transaction and execute any post-commit hooks.
        """
        def postCommit(ignored):
            for operation in self._postCommitOperations:
                operation()
            return ignored
        return self._sqlTxn.commit().addCallback(postCommit)


    def abort(self):
        """
        Abort the transaction.
        """
        return self._sqlTxn.abort()


    def eventsOlderThan(self, cutoff, batchSize=None):
        """
        Return up to the oldest batchSize events which exist completely earlier
        than "cutoff" (datetime)

        Returns a deferred to a list of (uid, calendarName, eventName, maxDate)
        tuples.
        """

        query = """
            select
                ch.OWNER_UID,
                cb.CALENDAR_RESOURCE_NAME,
                co.RESOURCE_NAME,
                max(tr.END_DATE)
            from
                TIME_RANGE tr,
                CALENDAR_BIND cb,
                CALENDAR_OBJECT co,
                CALENDAR_HOME ch
            where
                cb.BIND_MODE=%s AND
                cb.CALENDAR_RESOURCE_ID=tr.CALENDAR_RESOURCE_ID AND
                tr.CALENDAR_OBJECT_RESOURCE_ID=co.RESOURCE_ID AND
                ch.RESOURCE_ID=cb.CALENDAR_HOME_RESOURCE_ID
            group by
                ch.OWNER_UID,
                cb.CALENDAR_RESOURCE_NAME,
                co.RESOURCE_NAME
            having
                max(tr.END_DATE) < %s
            order by max(tr.END_DATE)
            """
        args = [_BIND_MODE_OWN, cutoff]
        if batchSize is not None:
            query += "limit %s"
            args.append(batchSize)

        return self.execSQL(query, args)


    @inlineCallbacks
    def removeOldEvents(self, cutoff, batchSize=None):
        """
        Remove up to batchSize events older than "cutoff" and return how
        many were removed.
        """

        results = (yield self.eventsOlderThan(cutoff, batchSize=batchSize))
        count = 0
        for uid, calendarName, eventName, maxDate in results:
            home = (yield self.calendarHomeWithUID(uid))
            calendar = (yield home.childWithName(calendarName))
            (yield calendar.removeObjectResourceWithName(eventName))
            count += 1
        returnValue(count)


    def _orphanedBase(limited):
        at = schema.ATTACHMENT
        co = schema.CALENDAR_OBJECT
        kwds = {}
        if limited:
            kwds["Limit"] = Parameter('batchSize')
        return Select(
            [at.DROPBOX_ID, at.PATH],
            From=at.join(co, at.DROPBOX_ID == co.DROPBOX_ID, "left outer"),
            Where=co.DROPBOX_ID == None,
            **kwds
        )

    _orphanedLimited = _orphanedBase(True)
    _orphanedUnlimited = _orphanedBase(False)
    del _orphanedBase


    def orphanedAttachments(self, batchSize=None):
        """
        Find attachments no longer referenced by any events.

        Returns a deferred to a list of (dropbox_id, path) tuples.
        """
        if batchSize is not None:
            kwds = {'batchSize': batchSize}
            query = self._orphanedLimited
        else:
            kwds = {}
            query = self._orphanedUnlimited
        return query.on(self, **kwds)


    @inlineCallbacks
    def removeOrphanedAttachments(self, batchSize=None):
        """
        Remove attachments that no longer have any references to them
        """

        # TODO: see if there is a better way to import Attachment
        from txdav.caldav.datastore.sql import Attachment

        results = (yield self.orphanedAttachments(batchSize=batchSize))
        count = 0
        for dropboxID, path in results:
            attachment = Attachment(self, dropboxID, path)
            (yield attachment.remove( ))
            count += 1
        returnValue(count)


class CommonHome(LoggingMixIn):

    # All these need to be initialized by derived classes for each store type
    _homeTable = None
    _homeMetaDataTable = None
    _childClass = None
    _childTable = None
    _bindTable = None
    _objectBindTable = None
    _notifierPrefix = None
    _revisionsTable = None
    _notificationRevisionsTable = NOTIFICATION_OBJECT_REVISIONS_TABLE
    
    _cacher = None  # Initialize in derived classes

    def __init__(self, transaction, ownerUID, notifiers):
        self._txn = transaction
        self._ownerUID = ownerUID
        self._resourceID = None
        self._shares = None
        self._childrenLoaded = False
        self._children = {}
        self._sharedChildren = {}
        self._notifiers = notifiers
        self._quotaUsedBytes = None

        # Needed for REVISION/BIND table join
        self._revisionBindJoinTable = {}
        for key, value in self._revisionsTable.iteritems():
            self._revisionBindJoinTable["REV:%s" % (key,)] = value
        for key, value in self._bindTable.iteritems():
            self._revisionBindJoinTable["BIND:%s" % (key,)] = value


    @classproperty
    def _resourceIDFromOwnerQuery(cls):
        home = cls._homeSchema
        return Select([home.RESOURCE_ID],
                      From=home, Where=home.OWNER_UID == Parameter("ownerUID"))

    @classproperty
    def _ownerFromFromResourceID(cls):
        home = cls._homeSchema
        return Select([home.OWNER_UID],
                      From=home,
                      Where=home.RESOURCE_ID == Parameter("resourceID"))

    @inlineCallbacks
    def initFromStore(self, no_cache=False):
        """
        Initialize this object from the store. We read in and cache all the
        extra meta-data from the DB to avoid having to do DB queries for those
        individually later.
        """
        result = yield self._cacher.get(self._ownerUID)
        if result is None:
            result = yield self._resourceIDFromOwnerQuery.on(
                self._txn, ownerUID=self._ownerUID)
            if result and not no_cache:
                yield self._cacher.set(self._ownerUID, result)

        if result:
            self._resourceID = result[0][0]
            yield self._loadPropertyStore()
            returnValue(self)
        else:
            returnValue(None)


    @classmethod
    @inlineCallbacks
    def homeWithUID(cls, txn, uid, create=False):
        if txn._notifierFactory:
            notifiers = (txn._notifierFactory.newNotifier(
                id=uid, prefix=cls._notifierPrefix
            ),)
        else:
            notifiers = None
        homeObject = cls(txn, uid, notifiers)
        homeObject = (yield homeObject.initFromStore())
        if homeObject is not None:
            returnValue(homeObject)
        else:
            if not create:
                returnValue(None)
            # Need to lock to prevent race condition

            # FIXME: this is an entire table lock - ideally we want a row lock
            # but the row does not exist yet. However, the "exclusive" mode does
            # allow concurrent reads so the only thing we block is other
            # attempts to provision a home, which is not too bad

            # Also note that we must not cache the owner_uid->resource_id
            # mapping in _cacher when creating as we don't want that to appear
            # until AFTER the commit

            yield Lock(cls._homeSchema, 'exclusive').on(txn)
            # Now test again
            exists = yield cls._resourceIDFromOwnerQuery.on(txn, ownerUID=uid)
            if not exists:
                resourceid = (yield Insert(
                    {cls._homeSchema.OWNER_UID: uid},
                    Return=cls._homeSchema.RESOURCE_ID).on(txn))[0][0]
                yield Insert(
                    {cls._homeMetaDataSchema.RESOURCE_ID: resourceid}).on(txn)
            home = cls(txn, uid, notifiers)
            home = (yield home.initFromStore(no_cache=not exists))
            if not exists:
                yield home.createdHome()
            returnValue(home)


    @classmethod
    @inlineCallbacks
    def homeUIDWithResourceID(cls, txn, rid):
        rows = (yield cls._ownerFromFromResourceID.on(txn, resourceID=rid))
        if rows:
            returnValue(rows[0][0])
        else:
            returnValue(None)


    def __repr__(self):
        return "<%s: %s>" % (self.__class__.__name__, self._resourceID)


    def uid(self):
        """
        Retrieve the unique identifier for this home.

        @return: a string.
        """
        return self._ownerUID


    def transaction(self):
        return self._txn


    def retrieveOldShares(self):
        return self._shares


    def name(self):
        """
        Implement L{IDataStoreResource.name} to return the uid.
        """
        return self.uid()


    @inlineCallbacks
    def children(self):
        """
        Retrieve children contained in this home.
        """
        x = []
        names = yield self.listChildren()
        for name in names:
            x.append((yield self.childWithName(name)))
        returnValue(x)


    @inlineCallbacks
    def loadChildren(self):
        """
        Load and cache all children - Depth:1 optimization
        """
        results1 = (yield self._childClass.loadAllObjects(self, owned=True))
        for result in results1:
            self._children[result.name()] = result
        results2 = (yield self._childClass.loadAllObjects(self, owned=False))
        for result in results2:
            self._sharedChildren[result.name()] = result
        self._childrenLoaded = True
        returnValue(results1 + results2)


    def listChildren(self):
        """
        Retrieve the names of the children in this home.

        @return: an iterable of C{str}s.
        """
        
        if self._childrenLoaded:
            return succeed(self._children.keys())
        else:
            return self._childClass.listObjects(self, owned=True)


    def listSharedChildren(self):
        """
        Retrieve the names of the children in this home.

        @return: an iterable of C{str}s.
        """
        if self._childrenLoaded:
            return succeed(self._sharedChildren.keys())
        else:
            return self._childClass.listObjects(self, owned=False)


    @memoizedKey("name", "_children")
    def childWithName(self, name):
        """
        Retrieve the child with the given C{name} contained in this
        home.

        @param name: a string.
        @return: an L{ICalendar} or C{None} if no such child exists.
        """
        return self._childClass.objectWithName(self, name, owned=True)

    @memoizedKey("resourceID", "_children")
    def childWithID(self, resourceID):
        """
        Retrieve the child with the given C{resourceID} contained in this
        home.

        @param name: a string.
        @return: an L{ICalendar} or C{None} if no such child exists.
        """
        return self._childClass.objectWithID(self, resourceID)

    @memoizedKey("name", "_sharedChildren")
    def sharedChildWithName(self, name):
        """
        Retrieve the shared child with the given C{name} contained in this
        home. Return a child object with this home and the name.

        IMPORTANT: take care when using this. Shared calendars should normally
        be accessed through the owner home collection, not the sharee home collection.
        The only reason for access through sharee home is to do some housekeeping
        for maintaining the revisions database to show shared calendars appearing and
        disappearing in the sharee home.

        @param name: a string.
        @return: an L{ICalendar} or C{None} if no such child
            exists.
        """
        return self._childClass.objectWithName(self, name, owned=False)


    @inlineCallbacks
    def createChildWithName(self, name):
        if name.startswith("."):
            raise HomeChildNameNotAllowedError(name)

        yield self._childClass.create(self, name)
        child = (yield self.childWithName(name))
        returnValue(child)

    def createdChild(self, child):
        pass


    @inlineCallbacks
    def removeChildWithName(self, name):
        child = yield self.childWithName(name)
        if child is None:
            raise NoSuchHomeChildError()

        try:
            yield child.remove()
        finally:
            self._children.pop(name, None)


    @classproperty
    def _syncTokenQuery(cls):
        """
        DAL Select statement to find the sync token.
        """
        rev = cls._revisionsSchema
        bind = cls._bindSchema
        return Select(
            [Max(rev.REVISION)],
            From=rev, Where=(
                rev.RESOURCE_ID.In(Select(
                    [bind.RESOURCE_ID], From=bind,
                    Where=bind.HOME_RESOURCE_ID == Parameter("resourceID")))
            ).Or((rev.HOME_RESOURCE_ID == Parameter("resourceID")).And(
                rev.RESOURCE_ID == None))
        )


    @inlineCallbacks
    def syncToken(self):
        revision = (yield self._syncTokenQuery.on(
            self._txn, resourceID=self._resourceID))[0][0]
        returnValue("%s#%s" % (self._resourceID, revision))


    @classproperty
    def _changesQuery(cls):
        bind = cls._bindSchema
        rev = cls._revisionsSchema
        return Select([bind.RESOURCE_NAME, rev.COLLECTION_NAME,
                       rev.RESOURCE_NAME, rev.DELETED],
                      From=rev.join(
                          bind,
                          (bind.HOME_RESOURCE_ID ==
                           Parameter("resourceID")).And(
                               rev.RESOURCE_ID ==
                               bind.RESOURCE_ID),
                          'left outer'),
                      Where=(rev.REVISION > Parameter("token")).And(
                          rev.HOME_RESOURCE_ID ==
                          Parameter("resourceID")))


    @inlineCallbacks
    def resourceNamesSinceToken(self, token, depth):

        results = [
            (
                path if path else (collection if collection else ""),
                name if name else "",
                wasdeleted
            )
            for path, collection, name, wasdeleted in
            (yield self._changesQuery.on(self._txn,
                                         resourceID=self._resourceID,
                                         token=token))
        ]

        deleted = []
        deleted_collections = set()
        changed_collections = set()
        for path, name, wasdeleted in results:
            if wasdeleted:
                if token:
                    deleted.append("%s/%s" % (path, name,))
                if not name:
                    deleted_collections.add(path)

        changed = []
        for path, name, wasdeleted in results:
            if path not in deleted_collections:
                changed.append("%s/%s" % (path, name,))
                if not name:
                    changed_collections.add(path)

        # Now deal with shared collections
        bind = self._bindSchema
        rev = self._revisionsSchema
        shares = yield self.listSharedChildren()
        for sharename in shares:
            sharetoken = 0 if sharename in changed_collections else token
            shareID = (yield Select(
                [bind.RESOURCE_ID], From=bind,
                Where=(bind.RESOURCE_NAME == sharename).And(
                    bind.HOME_RESOURCE_ID == self._resourceID).And(
                        bind.BIND_MODE == _BIND_MODE_OWN)
            ).on(self._txn))[0][0]
            results = [
                (
                    sharename,
                    name if name else "",
                    wasdeleted
                )
                for name, wasdeleted in
                (yield Select([rev.RESOURCE_NAME, rev.DELETED],
                                 From=rev,
                                Where=(rev.REVISION > sharetoken).And(
                                rev.RESOURCE_ID == shareID)).on(self._txn))
                if name
            ]

            for path, name, wasdeleted in results:
                if wasdeleted:
                    if sharetoken:
                        deleted.append("%s/%s" % (path, name,))

            for path, name, wasdeleted in results:
                changed.append("%s/%s" % (path, name,))

        changed.sort()
        deleted.sort()
        returnValue((changed, deleted))


    @inlineCallbacks
    def _loadPropertyStore(self):
        props = yield PropertyStore.load(
            self.uid(),
            self._txn,
            self._resourceID
        )
        self._propertyStore = props


    def properties(self):
        return self._propertyStore


    # IDataStoreResource
    def contentType(self):
        """
        The content type of objects
        """
        return None


    def md5(self):
        return None


    def size(self):
        return 0


    def created(self):
        return None


    def modified(self):
        return None


    @classproperty
    def _resourceByUIDQuery(cls):
        obj = cls._objectSchema
        bind = cls._bindSchema
        return Select([obj.PARENT_RESOURCE_ID, obj.RESOURCE_ID],
                     From=obj.join(bind, obj.PARENT_RESOURCE_ID ==
                                   bind.RESOURCE_ID),
                     Where=(obj.UID == Parameter("uid")).And(
                            bind.HOME_RESOURCE_ID == Parameter("resourceID")))


    @inlineCallbacks
    def objectResourcesWithUID(self, uid, ignore_children=()):
        """
        Return all child object resources with the specified UID, ignoring any
        in the named child collections.
        """
        results = []
        rows = (yield self._resourceByUIDQuery.on(self._txn, uid=uid,
                                                  resourceID=self._resourceID))
        if rows:
            for childID, objectID in rows:
                child = (yield self.childWithID(childID))
                if child and child.name() not in ignore_children:
                    objectResource = (yield child.objectResourceWithID(objectID))
                    results.append(objectResource)

        returnValue(results)


    @classproperty
    def _quotaQuery(cls):
        meta = cls._homeMetaDataSchema
        return Select(
            [meta.QUOTA_USED_BYTES], From=meta,
            Where=meta.RESOURCE_ID == Parameter("resourceID")
        )


    @inlineCallbacks
    def quotaUsedBytes(self):
        if self._quotaUsedBytes is None:
            self._quotaUsedBytes = (yield self._quotaQuery.on(
                self._txn, resourceID=self._resourceID))[0][0]
        returnValue(self._quotaUsedBytes)


    @classproperty
    def _preLockResourceIDQuery(cls):
        meta = cls._homeMetaDataSchema
        return Select(From=meta,
                      Where=meta.RESOURCE_ID==Parameter("resourceID"),
                      ForUpdate=True)


    @classproperty
    def _increaseQuotaQuery(cls):
        meta = cls._homeMetaDataSchema
        return Update({meta.QUOTA_USED_BYTES: meta.QUOTA_USED_BYTES +
                       Parameter("delta")},
                      Where=meta.RESOURCE_ID == Parameter("resourceID"),
                      Return=meta.QUOTA_USED_BYTES)


    @classproperty
    def _resetQuotaQuery(cls):
        meta = cls._homeMetaDataSchema
        return Update({meta.QUOTA_USED_BYTES: 0},
                      Where=meta.RESOURCE_ID == Parameter("resourceID"))


    @inlineCallbacks
    def adjustQuotaUsedBytes(self, delta):
        """
        Adjust quota used. We need to get a lock on the row first so that the
        adjustment is done atomically. It is import to do the 'select ... for
        update' because a race also exists in the 'update ... x = x + 1' case as
        seen via unit tests.
        """
        yield self._preLockResourceIDQuery.on(self._txn,
                                              resourceID=self._resourceID)

        self._quotaUsedBytes = (yield self._increaseQuotaQuery.on(
            self._txn, delta=delta, resourceID=self._resourceID))[0][0]

        # Double check integrity
        if self._quotaUsedBytes < 0:
            log.error(
                "Fixing quota adjusted below zero to %s by change amount %s" %
                (self._quotaUsedBytes, delta,))
            yield self._resetQuotaQuery.on(self._txn,
                                           resourceID=self._resourceID)
            self._quotaUsedBytes = 0


    def addNotifier(self, notifier):
        if self._notifiers is None:
            self._notifiers = ()
        self._notifiers += (notifier,)


    def notifierID(self, label="default"):
        if self._notifiers:
            return self._notifiers[0].getID(label)
        else:
            return None


    @inlineCallbacks
    def nodeName(self, label="default"):
        if self._notifiers:
            for notifier in self._notifiers:
                name = (yield notifier.nodeName(label=label))
                if name is not None:
                    returnValue(name)
        else:
            returnValue(None)

    def notifyChanged(self):
        """
        Trigger a notification of a change
        """
        if self._notifiers:
            for notifier in self._notifiers:
                self._txn.postCommit(notifier.notify)


class CommonHomeChild(LoggingMixIn, FancyEqMixin):
    """
    Common ancestor class of AddressBooks and Calendars.
    """

    compareAttributes = (
        "_name",
        "_home",
        "_resourceID",
    )

    _objectResourceClass = None

    _bindSchema           = None
    _homeChildSchema      = None
    _revisionsSchema      = None
    _objectSchema         = None

    _bindTable           = None
    _homeChildTable      = None
    _homeChildBindTable  = None
    _revisionsTable      = None
    _revisionsBindTable  = None
    _objectTable         = None


    def __init__(self, home, name, resourceID, owned):

        if home._notifiers:
            childID = "%s/%s" % (home.uid(), name)
            notifiers = [notifier.clone(label="collection", id=childID)
                         for notifier in home._notifiers]
        else:
            notifiers = None

        self._home              = home
        self._name              = name
        self._resourceID        = resourceID
        self._owned             = owned
        self._created           = None
        self._modified          = None
        self._objects           = {}
        self._objectNames       = None
        self._syncTokenRevision = None
        self._notifiers         = notifiers
        self._index             = None  # Derived classes need to set this
        self._invites           = None  # Derived classes need to set this


    @classproperty
    def _ownedChildListQuery(cls):
        bind = cls._bindSchema
        return Select([bind.RESOURCE_NAME], From=bind,
                      Where=(bind.HOME_RESOURCE_ID ==
                             Parameter("resourceID")).And(
                                 bind.BIND_MODE == _BIND_MODE_OWN))


    @classproperty
    def _sharedChildListQuery(cls):
        bind = cls._bindSchema
        return Select([bind.RESOURCE_NAME], From=bind,
                      Where=(bind.HOME_RESOURCE_ID ==
                             Parameter("resourceID")).And(
                                 bind.BIND_MODE != _BIND_MODE_OWN).And(
                                 bind.RESOURCE_NAME != None))


    @classmethod
    @inlineCallbacks
    def listObjects(cls, home, owned):
        """
        Retrieve the names of the children that exist in the given home.

        @return: an iterable of C{str}s.
        """
        # FIXME: tests don't cover this as directly as they should.
        if owned:
            rows = yield cls._ownedChildListQuery.on(
                home._txn, resourceID=home._resourceID)
        else:
            rows = yield cls._sharedChildListQuery.on(
                home._txn, resourceID=home._resourceID)
        names = [row[0] for row in rows]
        returnValue(names)


    @classmethod
    def _allHomeChildrenQuery(cls, owned):
        bind = cls._bindSchema
        child = cls._homeChildSchema
        if owned:
            ownedPiece = bind.BIND_MODE == _BIND_MODE_OWN
        else:
            ownedPiece = (bind.BIND_MODE != _BIND_MODE_OWN).And(
                bind.RESOURCE_NAME != None)
        return Select([child.RESOURCE_ID,
                       bind.RESOURCE_NAME,
                       child.CREATED,
                       child.MODIFIED],
                     From=child.join(
                         bind, child.RESOURCE_ID == bind.RESOURCE_ID,
                         'left outer'),
                     Where=(bind.HOME_RESOURCE_ID == Parameter("resourceID")
                           ).And(ownedPiece))


    @classproperty
    def _ownedHomeChildrenQuery(cls):
        return cls._allHomeChildrenQuery(True)


    @classproperty
    def _sharedHomeChildrenQuery(cls):
        return cls._allHomeChildrenQuery(False)


    @classmethod
    @inlineCallbacks
    def loadAllObjects(cls, home, owned):
        """
        Load all child objects and return a list of them. This must create the
        child classes and initialize them using "batched" SQL operations to keep
        this constant wrt the number of children. This is an optimization for
        Depth:1 operations on the home.
        """
        results = []

        # Load from the main table first
        if owned:
            query = cls._ownedHomeChildrenQuery
        else:
            query = cls._sharedHomeChildrenQuery
        dataRows = (yield query.on(home._txn, resourceID=home._resourceID))

        if dataRows:
            # Get property stores for all these child resources (if any found)
            propertyStores =(yield PropertyStore.loadAll(
                home.uid(),
                home._txn,
                cls._bindTable["name"],
                cls._bindTable["column_RESOURCE_ID"],
                cls._bindTable["column_HOME_RESOURCE_ID"],
                home._resourceID,
            ))

            bind = cls._bindSchema
            rev = cls._revisionsSchema
            if owned:
                ownedCond = bind.BIND_MODE == _BIND_MODE_OWN
            else:
                ownedCond = bind.BIND_MODE != _BIND_MODE_OWN
            revisions = (yield Select(
                [rev.RESOURCE_ID, Max(rev.REVISION)],
                From=rev.join(bind, rev.RESOURCE_ID == bind.RESOURCE_ID,
                              'left'),
                Where=(bind.HOME_RESOURCE_ID == home._resourceID).And(
                    ownedCond).And(
                        (rev.RESOURCE_NAME != None).Or(rev.DELETED == False)),
                GroupBy=rev.RESOURCE_ID
            ).on(home._txn))
            revisions = dict(revisions)

        # Create the actual objects merging in properties
        for resourceID, resource_name, created, modified in dataRows:
            child = cls(home, resource_name, resourceID, owned)
            child._created = created
            child._modified = modified
            child._syncTokenRevision = revisions[resourceID]
            propstore = propertyStores.get(resourceID, None)
            yield child._loadPropertyStore(propstore)
            results.append(child)
        returnValue(results)


    @classmethod
    def _homeChildLookup(cls, ownedPart):
        """
        Common portions of C{_ownedResourceIDByName}
        C{_resourceIDSharedToHomeByName}, except for the 'owned' fragment of the
        Where clause, supplied as an argument.
        """
        bind = cls._bindSchema
        return Select(
            [bind.RESOURCE_ID],
            From=bind,
            Where=(bind.RESOURCE_NAME == Parameter('objectName')).And(
                   bind.HOME_RESOURCE_ID == Parameter('homeID')).And(
                    ownedPart))


    @classproperty
    def _resourceIDOwnedByHomeByName(cls):
        """
        DAL query to look up an object resource ID owned by a home, given a
        resource name (C{objectName}), and a home resource ID
        (C{homeID}).
        """
        return cls._homeChildLookup(
            cls._bindSchema.BIND_MODE == _BIND_MODE_OWN)


    @classproperty
    def _resourceIDSharedToHomeByName(cls):
        """
        DAL query to look up an object resource ID shared to a home, given a
        resource name (C{objectName}), and a home resource ID
        (C{homeID}).
        """
        return cls._homeChildLookup(
            cls._bindSchema.BIND_MODE != _BIND_MODE_OWN)


    @classmethod
    @inlineCallbacks
    def objectWithName(cls, home, name, owned):
        """
        Retrieve the child with the given C{name} contained in the given
        C{home}.

        @param home: a L{CommonHome}.

        @param name: a string; the name of the L{CommonHomeChild} to retrieve.

        @param owned: a boolean - whether or not to get a shared child
        @return: an L{CommonHomeChild} or C{None} if no such child
            exists.
        """
        if owned:
            query = cls._resourceIDOwnedByHomeByName
        else:
            query = cls._resourceIDSharedToHomeByName
        data = yield query.on(home._txn,
                              objectName=name, homeID=home._resourceID)
        if not data:
            returnValue(None)
        resourceID = data[0][0]
        child = cls(home, name, resourceID, owned)
        yield child.initFromStore()
        returnValue(child)


    @classproperty
    def _homeChildByIDQuery(cls):
        """
        DAL query that looks up home child names / bind modes by home child
        resouce ID and home resource ID.
        """
        bind = cls._bindSchema
        return Select([bind.RESOURCE_NAME, bind.BIND_MODE],
                      From=bind,
                      Where=(bind.RESOURCE_ID == Parameter("resourceID")
                            ).And(bind.HOME_RESOURCE_ID == Parameter("homeID")))


    @classmethod
    @inlineCallbacks
    def objectWithID(cls, home, resourceID):
        """
        Retrieve the child with the given C{resourceID} contained in the given
        C{home}.

        @param home: a L{CommonHome}.
        @param resourceID: a string.
        @return: an L{CommonHomeChild} or C{None} if no such child
            exists.
        """
        data = yield cls._homeChildByIDQuery.on(
            home._txn, resourceID=resourceID, homeID=home._resourceID)
        if not data:
            returnValue(None)
        name, mode = data[0]
        child = cls(home, name, resourceID, mode == _BIND_MODE_OWN)
        yield child.initFromStore()
        returnValue(child)


    @classproperty
    def _insertDefaultHomeChild(cls):
        """
        DAL statement to create a home child with all default values.
        """
        child = cls._homeChildSchema
        return Insert({child.RESOURCE_ID: default},
                      Return=(child.RESOURCE_ID, child.CREATED, child.MODIFIED))


    @classproperty
    def _initialOwnerBind(cls):
        """
        DAL statement to create a bind entry for a particular home value.
        """
        bind = cls._bindSchema
        return Insert({bind.HOME_RESOURCE_ID: Parameter("homeID"),
                       bind.RESOURCE_ID: Parameter("resourceID"),
                       bind.RESOURCE_NAME: Parameter("name"),
                       bind.BIND_MODE: _BIND_MODE_OWN,
                       bind.SEEN_BY_OWNER: True,
                       bind.SEEN_BY_SHAREE: True,
                       bind.BIND_STATUS: _BIND_STATUS_ACCEPTED})


    @classmethod
    @inlineCallbacks
    def create(cls, home, name):
        child = (yield cls.objectWithName(home, name, owned=True))
        if child is not None:
            raise HomeChildNameAlreadyExistsError(name)

        if name.startswith("."):
            raise HomeChildNameNotAllowedError(name)

        # Create and initialize this object, similar to initFromStore
        resourceID, _created, _modified = (
            yield cls._insertDefaultHomeChild.on(home._txn))[0]

        # Bind table needs entry
        yield cls._initialOwnerBind.on(home._txn, homeID=home._resourceID,
                                       resourceID=resourceID, name=name)

        # Initialize other state
        child = cls(home, name, resourceID, True)
        child._created = _created
        child._modified = _modified
        yield child._loadPropertyStore()

        child.properties()[
            PropertyName.fromElement(ResourceType)
        ] = child.resourceType()
        yield child._initSyncToken()
        home.createdChild(child)

        # Change notification for a create is on the home collection
        home.notifyChanged()
        returnValue(child)


    @classproperty
    def _datesByIDQuery(cls):
        """
        DAL query to retrieve created/modified dates based on a resource ID.
        """
        child = cls._homeChildSchema
        return Select([child.CREATED, child.MODIFIED],
                      From=child,
                      Where=child.RESOURCE_ID == Parameter("resourceID"))


    @inlineCallbacks
    def initFromStore(self):
        """
        Initialise this object from the store, based on its already-populated
        resource ID. We read in and cache all the extra metadata from the DB to
        avoid having to do DB queries for those individually later.
        """
        self._created, self._modified = (
            yield self._datesByIDQuery.on(self._txn,
                                          resourceID=self._resourceID))[0]
        yield self._loadPropertyStore()


    @property
    def _txn(self):
        return self._home._txn


    def resourceType(self):
        return NotImplementedError


    def retrieveOldIndex(self):
        return self._index


    def retrieveOldInvites(self):
        return self._invites


    def __repr__(self):
        return "<%s: %s>" % (self.__class__.__name__, self._resourceID)


    def exists(self):
        """
        An empty resource-id means this object does not yet exist in the DB.
        """
        return self._resourceID is not None


    def name(self):
        return self._name


    @classproperty
    def _renameQuery(cls):
        """
        DAL statement to rename a L{CommonHomeChild}
        """
        bind = cls._bindSchema
        return Update({bind.RESOURCE_NAME: Parameter("name")},
                      Where=(bind.RESOURCE_ID == Parameter("resourceID")).And(
                          bind.HOME_RESOURCE_ID == Parameter("homeID")))


    @inlineCallbacks
    def rename(self, name):
        """
        Change the name of this L{CommonHomeChild} and update its sync token to
        reflect that change.

        @return: a L{Deferred} which fires when the modification is complete.
        """
        oldName = self._name
        yield self._renameQuery.on(self._txn, name=name,
                                   resourceID=self._resourceID,
                                   homeID=self._home._resourceID)
        self._name = name
        # update memos
        del self._home._children[oldName]
        self._home._children[name] = self
        yield self._renameSyncToken()

        self.notifyChanged()



    @classproperty
    def _deleteQuery(cls):
        """
        DAL statement to delete a L{CommonHomeChild} by its resource ID.
        """
        child = cls._homeChildSchema
        return Delete(child, Where=child.RESOURCE_ID == Parameter("resourceID"))


    @inlineCallbacks
    def remove(self):
        yield self._deletedSyncToken()
        yield self._deleteQuery.on(self._txn, NoSuchHomeChildError,
                                   resourceID=self._resourceID)
        # Set to non-existent state
        self._resourceID = None
        self._created    = None
        self._modified   = None
        self._objects    = {}

        self.notifyChanged()


    def ownerHome(self):
        return self._home


    @classproperty
    def _ownerHomeFromResourceQuery(cls):
        """
        DAL query to retrieve the home resource ID of the owner from the bound
        home-child ID.
        """
        bind = cls._bindSchema
        return Select([bind.HOME_RESOURCE_ID],
                     From=bind,
                     Where=(bind.RESOURCE_ID ==
                            Parameter("resourceID")).And(
                                bind.BIND_MODE == _BIND_MODE_OWN))


    @inlineCallbacks
    def sharerHomeID(self):
        if self._owned:
            # If this was loaded by its owner then we can skip the query, since
            # we already know who the owner is.
            returnValue(self._home._resourceID)
        else:
            rid = (yield self._ownerHomeFromResourceQuery.on(
                self._txn, resourceID=self._resourceID))[0][0]
            returnValue(rid)


    def setSharingUID(self, uid):
        self.properties()._setPerUserUID(uid)


    @inlineCallbacks
    def objectResources(self):
        """
        Load and cache all children - Depth:1 optimization
        """
        results = (yield self._objectResourceClass.loadAllObjects(self))
        for result in results:
            self._objects[result.name()] = result
            self._objects[result.uid()] = result
        self._objectNames = sorted([result.name() for result in results])
        returnValue(results)


    @inlineCallbacks
    def listObjectResources(self):
        if self._objectNames is None:
            rows = yield self._txn.execSQL(
                "select %(column_RESOURCE_NAME)s from %(name)s "
                "where %(column_PARENT_RESOURCE_ID)s = %%s" % self._objectTable,
                [self._resourceID])
            self._objectNames = sorted([row[0] for row in rows])

        returnValue(self._objectNames)


    def objectResourceWithName(self, name):
        if name in self._objects:
            return succeed(self._objects[name])
        else:
            return self._makeObjectResource(name=name)


    def objectResourceWithUID(self, uid):
        if uid in self._objects:
            return succeed(self._objects[uid])
        else:
            return self._makeObjectResource(uid=uid)


    def objectResourceWithID(self, resourceID):
        if resourceID in self._objects:
            return succeed(self._objects[resourceID])
        else:
            return self._makeObjectResource(resourceID=resourceID)

    @inlineCallbacks
    def _makeObjectResource(self, name=None, uid=None, resourceID=None):
        """
        We create the empty object first then have it initialize itself from the store
        """
        
        if resourceID:
            objectResource = (yield self._objectResourceClass.objectWithID(self, resourceID))
        else:
            objectResource = (yield self._objectResourceClass.objectWithName(self, name, uid))
        if objectResource:
            self._objects[objectResource.name()] = objectResource
            self._objects[objectResource.uid()] = objectResource
            self._objects[objectResource._resourceID] = objectResource
        else:
            if resourceID:
                self._objects[resourceID] = None
            else:
                self._objects[name if name else uid] = None
        returnValue(objectResource)


    @inlineCallbacks
    def resourceNameForUID(self, uid):
        try:
            resource = self._objects[uid]
            returnValue(resource.name() if resource else None)
        except KeyError:
            pass

        rows = yield self._txn.execSQL("""
            select %(column_RESOURCE_NAME)s
            from %(name)s
            where %(column_UID)s = %%s and %(column_PARENT_RESOURCE_ID)s = %%s
            """ % self._objectTable,
            [uid, self._resourceID]
        )
        if rows:
            returnValue(rows[0][0])
        else:
            self._objects[uid] = None
            returnValue(None)

    @inlineCallbacks
    def resourceUIDForName(self, name):
        try:
            resource = self._objects[name]
            returnValue(resource.uid() if resource else None)
        except KeyError:
            pass

        rows = yield self._txn.execSQL("""
            select %(column_UID)s
            from %(name)s
            where %(column_RESOURCE_NAME)s = %%s and %(column_PARENT_RESOURCE_ID)s = %%s
            """ % self._objectTable,
            [name, self._resourceID]
        )
        if rows:
            returnValue(rows[0][0])
        else:
            self._objects[name] = None
            returnValue(None)

    @inlineCallbacks
    def createObjectResourceWithName(self, name, component, metadata=None):
        """
        Create a new resource with component data and optional metadata. We create the
        python object using the metadata then create the actual store object with setComponent. 
        """
        if name in self._objects:
            if self._objects[name]:
                raise ObjectResourceNameAlreadyExistsError()

        objectResource = (yield self._objectResourceClass.create(self, name, component, metadata))
        self._objects[objectResource.name()] = objectResource
        self._objects[objectResource.uid()] = objectResource

        # Note: create triggers a notification when the component is set, so we don't need to
        # call notify( ) here like we do for object removal.

        returnValue(objectResource)

    @inlineCallbacks
    def removeObjectResourceWithName(self, name):

        uid = (yield self._txn.execSQL(
            "delete from %(name)s "
            "where %(column_RESOURCE_NAME)s = %%s and %(column_PARENT_RESOURCE_ID)s = %%s "
            "returning %(column_UID)s" % self._objectTable,
            [name, self._resourceID],
            raiseOnZeroRowCount=lambda:NoSuchObjectResourceError()
        ))[0][0]
        self._objects.pop(name, None)
        self._objects.pop(uid, None)
        yield self._deleteRevision(name)

        self.notifyChanged()


    @inlineCallbacks
    def removeObjectResourceWithUID(self, uid):

        name = (yield self._txn.execSQL(
            "delete from %(name)s "
            "where %(column_UID)s = %%s and %(column_PARENT_RESOURCE_ID)s = %%s "
            "returning %(column_RESOURCE_NAME)s" % self._objectTable,
            [uid, self._resourceID],
            raiseOnZeroRowCount=lambda:NoSuchObjectResourceError()
        ))[0][0]
        self._objects.pop(name, None)
        self._objects.pop(uid, None)
        yield self._deleteRevision(name)

        self.notifyChanged()


    @inlineCallbacks
    def syncToken(self):
        if self._syncTokenRevision is None:
            self._syncTokenRevision = (yield self._txn.execSQL(
                """
                select max(%(column_REVISION)s) from %(name)s
                where %(column_RESOURCE_ID)s = %%s
                """ % self._revisionsTable,
                [self._resourceID,]
            ))[0][0]
        returnValue(("%s#%s" % (self._resourceID, self._syncTokenRevision,)))


    def objectResourcesSinceToken(self, token):
        raise NotImplementedError()


    @inlineCallbacks
    def resourceNamesSinceToken(self, token):
        results = [
            (name if name else "", deleted)
            for name, deleted in
            (yield self._txn.execSQL("""
                select %(column_RESOURCE_NAME)s, %(column_DELETED)s from %(name)s
                where %(column_REVISION)s > %%s and %(column_RESOURCE_ID)s = %%s
                """ % self._revisionsTable,
                [token, self._resourceID],
            ))
        ]
        results.sort(key=lambda x:x[1])

        changed = []
        deleted = []
        for name, wasdeleted in results:
            if name:
                if wasdeleted:
                    if token:
                        deleted.append(name)
                else:
                    changed.append(name)

        returnValue((changed, deleted))


    @inlineCallbacks
    def _initSyncToken(self):

        # Remove any deleted revision entry that uses the same name
        yield self._txn.execSQL("""
            delete from %(name)s
            where %(column_HOME_RESOURCE_ID)s = %%s and %(column_COLLECTION_NAME)s = %%s
            """ % self._revisionsTable,
            [self._home._resourceID, self._name]
        )

        # Insert new entry
        self._syncTokenRevision = (yield self._txn.execSQL("""
            insert into %(name)s
            (%(column_HOME_RESOURCE_ID)s, %(column_RESOURCE_ID)s, %(column_COLLECTION_NAME)s, %(column_RESOURCE_NAME)s, %(column_REVISION)s, %(column_DELETED)s)
            values (%%s, %%s, %%s, null, nextval('%(sequence)s'), FALSE)
            returning %(column_REVISION)s
            """ % self._revisionsTable,
            [self._home._resourceID, self._resourceID, self._name]
        ))[0][0]


    @inlineCallbacks
    def _updateSyncToken(self):

        self._syncTokenRevision = (yield self._txn.execSQL("""
            update %(name)s
            set (%(column_REVISION)s) = (nextval('%(sequence)s'))
            where %(column_RESOURCE_ID)s = %%s and %(column_RESOURCE_NAME)s is null
            returning %(column_REVISION)s
            """ % self._revisionsTable,
            [self._resourceID,]
        ))[0][0]


    @inlineCallbacks
    def _renameSyncToken(self):

        self._syncTokenRevision = (yield self._txn.execSQL("""
            update %(name)s
            set (%(column_REVISION)s, %(column_COLLECTION_NAME)s) = (nextval('%(sequence)s'), %%s)
            where %(column_RESOURCE_ID)s = %%s and %(column_RESOURCE_NAME)s is null
            returning %(column_REVISION)s
            """ % self._revisionsTable,
            [self._name, self._resourceID,]
        ))[0][0]


    @inlineCallbacks
    def _deletedSyncToken(self, sharedRemoval=False):

        # Remove all child entries
        yield self._txn.execSQL("""
            delete from %(name)s
            where %(column_HOME_RESOURCE_ID)s = %%s and %(column_RESOURCE_ID)s = %%s and %(column_COLLECTION_NAME)s is null
            """ % self._revisionsTable,
            [self._home._resourceID, self._resourceID,]
        )

        # If this is a share being removed then we only mark this one specific home/resource-id as being deleted.
        # On the other hand, if it is a non-shared collection, then we need to mark all collections
        # with the resource-id as being deleted to account for direct shares.
        if sharedRemoval:
            yield self._txn.execSQL("""
                update %(name)s
                set (%(column_RESOURCE_ID)s, %(column_REVISION)s, %(column_DELETED)s)
                 = (null, nextval('%(sequence)s'), TRUE)
                where %(column_HOME_RESOURCE_ID)s = %%s and %(column_RESOURCE_ID)s = %%s and %(column_RESOURCE_NAME)s is null
                returning %(column_REVISION)s
                """ % self._revisionsTable,
                [self._home._resourceID, self._resourceID,]
            )
        else:
            yield self._txn.execSQL("""
                update %(name)s
                set (%(column_RESOURCE_ID)s, %(column_REVISION)s, %(column_DELETED)s)
                 = (null, nextval('%(sequence)s'), TRUE)
                where %(column_RESOURCE_ID)s = %%s and %(column_RESOURCE_NAME)s is null
                returning %(column_REVISION)s
                """ % self._revisionsTable,
                [self._resourceID,]
            )
        self._syncTokenRevision = None


    def _insertRevision(self, name):
        return self._changeRevision("insert", name)

    def _updateRevision(self, name):
        return self._changeRevision("update", name)

    def _deleteRevision(self, name):
        return self._changeRevision("delete", name)


    @inlineCallbacks
    def _changeRevision(self, action, name):

        if action == "delete":
            self._syncTokenRevision = (yield self._txn.execSQL("""
                update %(name)s
                set (%(column_REVISION)s, %(column_DELETED)s) = (nextval('%(sequence)s'), TRUE)
                where %(column_RESOURCE_ID)s = %%s and %(column_RESOURCE_NAME)s = %%s
                returning %(column_REVISION)s
                """ % self._revisionsTable,
                [self._resourceID, name]
            ))[0][0]
        elif action == "update":
            self._syncTokenRevision = (yield self._txn.execSQL("""
                update %(name)s
                set (%(column_REVISION)s) = (nextval('%(sequence)s'))
                where %(column_RESOURCE_ID)s = %%s and %(column_RESOURCE_NAME)s = %%s
                returning %(column_REVISION)s
                """ % self._revisionsTable,
                [self._resourceID, name]
            ))[0][0]
        elif action == "insert":
            # Note that an "insert" may happen for a resource that previously existed and then
            # was deleted. In that case an entry in the REVISIONS table still exists so we have to
            # detect that and do db INSERT or UPDATE as appropriate

            found = bool( (yield self._txn.execSQL("""
                select %(column_RESOURCE_ID)s from %(name)s
                where %(column_RESOURCE_ID)s = %%s and %(column_RESOURCE_NAME)s = %%s
                """ % self._revisionsTable,
                [self._resourceID, name, ]
            )) )
            if found:
                self._syncTokenRevision = (yield self._txn.execSQL("""
                    update %(name)s
                    set (%(column_REVISION)s, %(column_DELETED)s) = (nextval('%(sequence)s'), FALSE)
                    where %(column_RESOURCE_ID)s = %%s and %(column_RESOURCE_NAME)s = %%s
                    returning %(column_REVISION)s
                    """ % self._revisionsTable,
                    [self._resourceID, name]
                ))[0][0]
            else:
                self._syncTokenRevision = (yield self._txn.execSQL("""
                    insert into %(name)s
                    (%(column_HOME_RESOURCE_ID)s, %(column_RESOURCE_ID)s, %(column_RESOURCE_NAME)s, %(column_REVISION)s, %(column_DELETED)s)
                    values (%%s, %%s, %%s, nextval('%(sequence)s'), FALSE)
                    returning %(column_REVISION)s
                    """ % self._revisionsTable,
                    [self._home._resourceID, self._resourceID, name]
                ))[0][0]

    def objectResourcesHaveProperties(self):
        return False

    @inlineCallbacks
    def _loadPropertyStore(self, props=None):
        if props is None:
            props = yield PropertyStore.load(
                self.ownerHome().uid(),
                self._txn,
                self._resourceID
            )
        self.initPropertyStore(props)
        self._properties = props


    def properties(self):
        return self._properties


    def initPropertyStore(self, props):
        """
        A hook for subclasses to override in order to set up their property
        store after it's been created.

        @param props: the L{PropertyStore} from C{properties()}.
        """


    def _doValidate(self, component):
        raise NotImplementedError


    # IDataStoreResource
    def contentType(self):
        raise NotImplementedError()


    def md5(self):
        return None


    def size(self):
        return 0


    def created(self):
        return datetimeMktime(parseSQLTimestamp(self._created)) if self._created else None


    def modified(self):
        return datetimeMktime(parseSQLTimestamp(self._modified)) if self._modified else None


    def addNotifier(self, notifier):
        if self._notifiers is None:
            self._notifiers = ()
        self._notifiers += (notifier,)
 
    def notifierID(self, label="default"):
        if self._notifiers:
            return self._notifiers[0].getID(label)
        else:
            return None

    @inlineCallbacks
    def nodeName(self, label="default"):
        if self._notifiers:
            for notifier in self._notifiers:
                name = (yield notifier.nodeName(label=label))
                if name is not None:
                    returnValue(name)
        else:
            returnValue(None)

    def notifyChanged(self):
        """
        Trigger a notification of a change
        """
        if self._notifiers:
            for notifier in self._notifiers:
                self._txn.postCommit(notifier.notify)



class CommonObjectResource(LoggingMixIn, FancyEqMixin):
    """
    @ivar _path: The path of the file on disk

    @type _path: L{FilePath}
    """

    compareAttributes = (
        "_name",
        "_parentCollection",
    )

    _objectTable = None

    def __init__(self, parent, name, uid, resourceID=None, metadata=None):
        self._parentCollection = parent
        self._resourceID = resourceID
        self._name = name
        self._uid = uid
        self._md5 = None
        self._size = None
        self._created = None
        self._modified = None
        self._objectText = None


    @classmethod
    @inlineCallbacks
    def loadAllObjects(cls, parent):
        """
        Load all child objects and return a list of them. This must create the child classes
        and initialize them using "batched" SQL operations to keep this constant wrt the number of
        children. This is an optimization for Depth:1 operations on the collection.
        """
        
        results = []

        # Load from the main table first
        dataRows = yield parent._txn.execSQL(cls._selectAllColumns() + """
            from %(name)s
            where %(column_PARENT_RESOURCE_ID)s = %%s
            """ % cls._objectTable,
            [parent._resourceID,]
        )
        
        if dataRows:
            # Get property stores for all these child resources (if any found)
            if parent.objectResourcesHaveProperties():
                propertyStores =(yield PropertyStore.loadAll(
                    parent._home.uid(),
                    parent._txn,
                    cls._objectTable["name"],
                    "%s.%s" % (cls._objectTable["name"], cls._objectTable["column_RESOURCE_ID"],),
                    "%s.%s" % (cls._objectTable["name"], cls._objectTable["column_PARENT_RESOURCE_ID"]),
                    parent._resourceID,
                ))
            else:
                propertyStores = {}
        
        # Create the actual objects merging in properties
        for row in dataRows:
            child = cls(parent, "", None)
            child._initFromRow(tuple(row))
            yield child._loadPropertyStore(props=propertyStores.get(child._resourceID, None))
            results.append(child)
        
        returnValue(results)

    @classmethod
    def objectWithName(cls, parent, name, uid):
        objectResource = cls(parent, name, uid, None)
        return objectResource.initFromStore()

    @classmethod
    def objectWithID(cls, parent, resourceID):
        objectResource = cls(parent, None, None, resourceID)
        return objectResource.initFromStore()

    @classmethod
    @inlineCallbacks
    def create(cls, parent, name, component, metadata):

        child = (yield cls.objectWithName(parent, name, None))
        if child:
            raise ObjectResourceNameAlreadyExistsError(name)

        if name.startswith("."):
            raise ObjectResourceNameNotAllowedError(name)
        
        objectResource = cls(parent, name, None, None, metadata=metadata)
        yield objectResource.setComponent(component, inserting=True)
        yield objectResource._loadPropertyStore(created=True)

        # Note: setComponent triggers a notification, so we don't need to
        # call notify( ) here like we do for object removal.
        
        returnValue(objectResource)

    @inlineCallbacks
    def initFromStore(self):
        """
        Initialise this object from the store. We read in and cache all the extra metadata
        from the DB to avoid having to do DB queries for those individually later. Either the
        name or uid is present, so we have to tweak the query accordingly.

        @return: L{self} if object exists in the DB, else C{None}
        """

        if self._name:
            rows = yield self._txn.execSQL(self._selectAllColumns() + """
                from %(name)s
                where %(column_RESOURCE_NAME)s = %%s and %(column_PARENT_RESOURCE_ID)s = %%s
                """ % self._objectTable,
                [self._name, self._parentCollection._resourceID]
            )
        elif self._uid:
            rows = yield self._txn.execSQL(self._selectAllColumns() + """
                from %(name)s
                where %(column_UID)s = %%s and %(column_PARENT_RESOURCE_ID)s = %%s
                """ % self._objectTable,
                [self._uid, self._parentCollection._resourceID]
            )
        elif self._resourceID:
            rows = yield self._txn.execSQL(self._selectAllColumns() + """
                from %(name)s
                where %(column_RESOURCE_ID)s = %%s and %(column_PARENT_RESOURCE_ID)s = %%s
                """ % self._objectTable,
                [self._resourceID, self._parentCollection._resourceID]
            )
        if rows:
            self._initFromRow(tuple(rows[0]))
            yield self._loadPropertyStore()
            returnValue(self)
        else:
            returnValue(None)

    @classmethod
    def _selectAllColumns(cls):
        """
        Full set of columns in the object table that need to be loaded to
        initialize the object resource state.
        """
        return """
            select
              %(column_RESOURCE_ID)s,
              %(column_RESOURCE_NAME)s,
              %(column_UID)s,
              %(column_MD5)s,
              character_length(%(column_TEXT)s),
              %(column_CREATED)s,
              %(column_MODIFIED)s
        """ % cls._objectTable

    def _initFromRow(self, row):
        """
        Given a select result using the columns from L{_selectAllColumns}, initialize
        the object resource state.
        """
        (self._resourceID,
         self._name,
         self._uid,
         self._md5,
         self._size,
         self._created,
         self._modified,) = tuple(row)

    @inlineCallbacks
    def _loadPropertyStore(self, props=None, created=False):
        if props is None:
            if self._parentCollection.objectResourcesHaveProperties():
                props = yield PropertyStore.load(
                    self._parentCollection.ownerHome().uid(),
                    self._txn,
                    self._resourceID,
                    created=created
                )
            else:
                props = NonePropertyStore(self._parentCollection.ownerHome().uid())
        self.initPropertyStore(props)
        self._propertyStore = props

    
    def properties(self):
        return self._propertyStore


    def initPropertyStore(self, props):
        """
        A hook for subclasses to override in order to set up their property
        store after it's been created.

        @param props: the L{PropertyStore} from C{properties()}.
        """

    def __repr__(self):
        return "<%s: %s>" % (self.__class__.__name__, self._resourceID)


    @property
    def _txn(self):
        return self._parentCollection._txn


    def setComponent(self, component, inserting=False):
        raise NotImplementedError


    def component(self):
        raise NotImplementedError


    @inlineCallbacks
    def componentType(self):
        returnValue((yield self.component()).mainType())


    def uid(self):
        return self._uid


    def name(self):
        return self._name



    # IDataStoreResource
    def contentType(self):
        raise NotImplementedError()


    def md5(self):
        return self._md5


    def size(self):
        return self._size


    def created(self):
        return datetimeMktime(parseSQLTimestamp(self._created))


    def modified(self):
        return datetimeMktime(parseSQLTimestamp(self._modified))


    @inlineCallbacks
    def text(self):
        if self._objectText is None:
            text = (yield self._txn.execSQL(
                "select %(column_TEXT)s from %(name)s where "
                "%(column_RESOURCE_ID)s = %%s" % self._objectTable,
                [self._resourceID]
            ))[0][0]
            self._objectText = text
            returnValue(text)
        else:
            returnValue(self._objectText)



class NotificationCollection(LoggingMixIn, FancyEqMixin):

    implements(INotificationCollection)

    compareAttributes = (
        "_uid",
        "_resourceID",
    )

    _revisionsTable = NOTIFICATION_OBJECT_REVISIONS_TABLE

    def __init__(self, txn, uid, resourceID):

        self._txn = txn
        self._uid = uid
        self._resourceID = resourceID
        self._notifications = {}
        self._notificationNames = None
        self._syncTokenRevision = None

        # Make sure we have push notifications setup to push on this collection
        # as well as the home it is in
        if txn._notifierFactory:
            childID = "%s/%s" % (uid, "notification")
            notifier = txn._notifierFactory.newNotifier(
                label="collection",
                id=childID,
                prefix=txn._homeClass[txn._primaryHomeType]._notifierPrefix
            )
            notifier.addID(id=uid)
            notifiers = (notifier,)
        else:
            notifiers = None
        self._notifiers = notifiers

    @classmethod
    @inlineCallbacks
    def notificationsWithUID(cls, txn, uid):
        """
        Implement notificationsWithUID.
        """
        rows = yield txn.execSQL(
            """
            select %(column_RESOURCE_ID)s from %(name)s where
            %(column_OWNER_UID)s = %%s
            """ % NOTIFICATION_HOME_TABLE, [uid]
        )
        if rows:
            resourceID = rows[0][0]
            created = False
        else:
            resourceID = str((yield txn.execSQL(
                "insert into %(name)s (%(column_OWNER_UID)s) values (%%s) returning %(column_RESOURCE_ID)s" % NOTIFICATION_HOME_TABLE,
                [uid]
            ))[0][0])
            created = True
        collection = cls(txn, uid, resourceID)
        yield collection._loadPropertyStore()
        if created:
            yield collection._initSyncToken()
        returnValue(collection)

    @inlineCallbacks
    def _loadPropertyStore(self):
        self._propertyStore = yield PropertyStore.load(
            self._uid,
            self._txn,
            self._resourceID
        )


    def resourceType(self):
        return ResourceType.notification #@UndefinedVariable

    def retrieveOldIndex(self):
        return PostgresLegacyNotificationsEmulator(self)

    def __repr__(self):
        return "<%s: %s>" % (self.__class__.__name__, self._resourceID)

    def name(self):
        return "notification"

    def uid(self):
        return self._uid


    @inlineCallbacks
    def notificationObjects(self):
        results = (yield NotificationObject.loadAllObjects(self))
        for result in results:
            self._notifications[result.uid()] = result
        self._notificationNames = sorted([result.name() for result in results])
        returnValue(results)


    @inlineCallbacks
    def listNotificationObjects(self):
        if self._notificationNames is None:
            rows = yield self._txn.execSQL(
                "select (NOTIFICATION_UID) from NOTIFICATION "
                "where NOTIFICATION_HOME_RESOURCE_ID = %s",
                [self._resourceID])
            self._notificationNames = sorted([row[0] for row in rows])
        returnValue(self._notificationNames)


    def _nameToUID(self, name):
        """
        Based on the file-backed implementation, the 'name' is just uid +
        ".xml".
        """
        return name.rsplit(".", 1)[0]


    def notificationObjectWithName(self, name):
        return self.notificationObjectWithUID(self._nameToUID(name))


    @memoizedKey("uid", "_notifications")
    @inlineCallbacks
    def notificationObjectWithUID(self, uid):
        """
        We create the empty object first then have it initialize itself from the store
        """

        no = NotificationObject(self, uid)
        no = (yield no.initFromStore())
        returnValue(no)


    @inlineCallbacks
    def writeNotificationObject(self, uid, xmltype, xmldata):

        inserting = False
        notificationObject = yield self.notificationObjectWithUID(uid)
        if notificationObject is None:
            notificationObject = NotificationObject(self, uid)
            inserting = True
        yield notificationObject.setData(uid, xmltype, xmldata, inserting=inserting)
        if inserting:
            yield self._insertRevision("%s.xml" % (uid,))
        else:
            yield self._updateRevision("%s.xml" % (uid,))


    def removeNotificationObjectWithName(self, name):
        return self.removeNotificationObjectWithUID(self._nameToUID(name))


    @inlineCallbacks
    def removeNotificationObjectWithUID(self, uid):
        yield self._txn.execSQL(
            "delete from NOTIFICATION "
            "where NOTIFICATION_UID = %s and NOTIFICATION_HOME_RESOURCE_ID = %s",
            [uid, self._resourceID]
        )
        self._notifications.pop(uid, None)
        yield self._deleteRevision("%s.xml" % (uid,))


    @inlineCallbacks
    def _initSyncToken(self):
        self._syncTokenRevision = (yield self._txn.execSQL("""
            insert into %(name)s
            (%(column_HOME_RESOURCE_ID)s, %(column_RESOURCE_NAME)s, %(column_REVISION)s, %(column_DELETED)s)
            values (%%s, null, nextval('%(sequence)s'), FALSE)
            returning %(column_REVISION)s
            """ % self._revisionsTable,
            [self._resourceID,]
        ))[0][0]


    @inlineCallbacks
    def syncToken(self):
        if self._syncTokenRevision is None:
            self._syncTokenRevision = (yield self._txn.execSQL(
                """
                select max(%(column_REVISION)s) from %(name)s
                where %(column_HOME_RESOURCE_ID)s = %%s
                """ % self._revisionsTable,
                [self._resourceID,]
            ))[0][0]
        returnValue("%s#%s" % (self._resourceID, self._syncTokenRevision,))


    def objectResourcesSinceToken(self, token):
        raise NotImplementedError()


    @inlineCallbacks
    def resourceNamesSinceToken(self, token):
        results = [
            (name if name else "", deleted)
            for name, deleted in
            (yield self._txn.execSQL("""
                select %(column_RESOURCE_NAME)s, %(column_DELETED)s from %(name)s
                where %(column_REVISION)s > %%s and %(column_HOME_RESOURCE_ID)s = %%s
                """ % self._revisionsTable,
                [token, self._resourceID],
            ))
        ]
        results.sort(key=lambda x:x[1])

        changed = []
        deleted = []
        for name, wasdeleted in results:
            if name:
                if wasdeleted:
                    if token:
                        deleted.append(name)
                else:
                    changed.append(name)

        returnValue((changed, deleted))


    def _updateSyncToken(self):
        self._syncTokenRevision =  self._txn.execSQL("""
            update %(name)s
            set (%(column_REVISION)s) = (nextval('%(sequence)s'))
            where %(column_HOME_RESOURCE_ID)s = %%s and %(column_RESOURCE_NAME)s is null
            returning %(column_REVISION)s
            """ % self._revisionsTable,
            [self._resourceID,]
        )[0][0]


    def _insertRevision(self, name):
        return self._changeRevision("insert", name)


    def _updateRevision(self, name):
        return self._changeRevision("update", name)


    def _deleteRevision(self, name):
        return self._changeRevision("delete", name)


    @inlineCallbacks
    def _changeRevision(self, action, name):

        if action == "delete":
            self._syncTokenRevision = (yield self._txn.execSQL("""
                update %(name)s
                set (%(column_REVISION)s, %(column_DELETED)s) = (nextval('%(sequence)s'), TRUE)
                where %(column_HOME_RESOURCE_ID)s = %%s and %(column_RESOURCE_NAME)s = %%s
                returning %(column_REVISION)s
                """ % self._revisionsTable,
                [self._resourceID, name]
            ))[0][0]
        elif action == "update":
            self._syncTokenRevision = (yield self._txn.execSQL("""
                update %(name)s
                set (%(column_REVISION)s) = (nextval('%(sequence)s'))
                where %(column_HOME_RESOURCE_ID)s = %%s and %(column_RESOURCE_NAME)s = %%s
                returning %(column_REVISION)s
                """ % self._revisionsTable,
                [self._resourceID, name]
            ))[0][0]
        elif action == "insert":
            # Note that an "insert" may happen for a resource that previously existed and then
            # was deleted. In that case an entry in the REVISIONS table still exists so we have to
            # detect that and do db INSERT or UPDATE as appropriate

            found = bool( (yield self._txn.execSQL("""
                select %(column_HOME_RESOURCE_ID)s from %(name)s
                where %(column_HOME_RESOURCE_ID)s = %%s and %(column_RESOURCE_NAME)s = %%s
                """ % self._revisionsTable,
                [self._resourceID, name, ]
            )))
            if found:
                self._syncTokenRevision = (yield self._txn.execSQL("""
                    update %(name)s
                    set (%(column_REVISION)s, %(column_DELETED)s) = (nextval('%(sequence)s'), FALSE)
                    where %(column_HOME_RESOURCE_ID)s = %%s and %(column_RESOURCE_NAME)s = %%s
                    returning %(column_REVISION)s
                    """ % self._revisionsTable,
                    [self._resourceID, name]
                ))[0][0]
            else:
                self._syncTokenRevision = (yield self._txn.execSQL("""
                    insert into %(name)s
                    (%(column_HOME_RESOURCE_ID)s, %(column_RESOURCE_NAME)s, %(column_REVISION)s, %(column_DELETED)s)
                    values (%%s, %%s, nextval('%(sequence)s'), FALSE)
                    returning %(column_REVISION)s
                    """ % self._revisionsTable,
                    [self._resourceID, name,]
                ))[0][0]

        self.notifyChanged()


    def properties(self):
        return self._propertyStore


    def notifierID(self, label="default"):
        if self._notifiers:
            return self._notifiers[0].getID(label)
        else:
            return None

    @inlineCallbacks
    def nodeName(self, label="default"):
        if self._notifiers:
            for notifier in self._notifiers:
                name = (yield notifier.nodeName(label=label))
                if name is not None:
                    returnValue(name)
        else:
            returnValue(None)

    def notifyChanged(self):
        """
        Trigger a notification of a change
        """
        if self._notifiers:
            for notifier in self._notifiers:
                self._txn.postCommit(notifier.notify)


class NotificationObject(LoggingMixIn, FancyEqMixin):

    implements(INotificationObject)

    compareAttributes = (
        "_resourceID",
        "_home",
    )

    def __init__(self, home, uid):
        self._home = home
        self._resourceID = None
        self._uid = uid
        self._md5 = None
        self._size = None
        self._created = None
        self._modified = None
        self._xmlType = None
        self._objectText = None

    def __repr__(self):
        return "<%s: %s>" % (self.__class__.__name__, self._resourceID)

    @classmethod
    @inlineCallbacks
    def loadAllObjects(cls, parent):
        """
        Load all child objects and return a list of them. This must create the child classes
        and initialize them using "batched" SQL operations to keep this constant wrt the number of
        children. This is an optimization for Depth:1 operations on the collection.
        """
        
        results = []

        # Load from the main table first
        dataRows = (yield parent._txn.execSQL("""
            select
                RESOURCE_ID,
                NOTIFICATION_UID,
                MD5,
                character_length(XML_DATA),
                XML_TYPE,
                CREATED,
                MODIFIED
            from NOTIFICATION
            where NOTIFICATION_HOME_RESOURCE_ID = %s
            """,
            [parent._resourceID]
        ))
        
        if dataRows:
            # Get property stores for all these child resources (if any found)
            propertyStores =(yield PropertyStore.loadAll(
                parent.uid(),
                parent._txn,
                "NOTIFICATION",
                "NOTIFICATION.RESOURCE_ID",
                "NOTIFICATION.NOTIFICATION_HOME_RESOURCE_ID",
                parent._resourceID,
            ))
        
        # Create the actual objects merging in properties
        for row in dataRows:
            child = cls(parent, None)
            (child._resourceID,
             child._uid,
             child._md5,
             child._size,
             child._xmlType,
             child._created,
             child._modified,) = tuple(row)
            child._loadPropertyStore(props=propertyStores.get(child._resourceID, None))
            results.append(child)
        
        returnValue(results)

    @inlineCallbacks
    def initFromStore(self):
        """
        Initialise this object from the store. We read in and cache all the extra metadata
        from the DB to avoid having to do DB queries for those individually later.

        @return: L{self} if object exists in the DB, else C{None}
        """
        rows = (yield self._txn.execSQL("""
            select
                RESOURCE_ID,
                MD5,
                character_length(XML_DATA),
                XML_TYPE,
                CREATED,
                MODIFIED
            from NOTIFICATION
            where NOTIFICATION_UID = %s and NOTIFICATION_HOME_RESOURCE_ID = %s
            """,
            [self._uid, self._home._resourceID]))
        if rows:
            (self._resourceID,
             self._md5,
             self._size,
             self._xmlType,
             self._created,
             self._modified,) = tuple(rows[0])
            self._loadPropertyStore()
            returnValue(self)
        else:
            returnValue(None)

    def _loadPropertyStore(self, props=None, created=False):
        if props is None:
            props = NonePropertyStore(self._home.uid())
        self._propertyStore = props


    def properties(self):
        return self._propertyStore


    @property
    def _txn(self):
        return self._home._txn


    def notificationCollection(self):
        return self._home


    def uid(self):
        return self._uid


    def name(self):
        return self.uid() + ".xml"


    @inlineCallbacks
    def setData(self, uid, xmltype, xmldata, inserting=False):
        """
        Set the object resource data and update and cached metadata.
        """

        self._xmlType = NotificationType(xmltype)
        self._md5 = hashlib.md5(xmldata).hexdigest()
        self._size = len(xmldata)
        if inserting:
            rows = yield self._txn.execSQL("""
                insert into NOTIFICATION
                  (NOTIFICATION_HOME_RESOURCE_ID, NOTIFICATION_UID, XML_TYPE, XML_DATA, MD5)
                values
                  (%s, %s, %s, %s, %s)
                returning
                  RESOURCE_ID,
                  CREATED,
                  MODIFIED
                """,
                [self._home._resourceID, uid, self._xmlType.toxml(), xmldata, self._md5]
            )
            self._resourceID, self._created, self._modified = rows[0]
            self._loadPropertyStore()
        else:
            rows = yield self._txn.execSQL("""
                update NOTIFICATION
                set XML_TYPE = %s, XML_DATA = %s, MD5 = %s
                where NOTIFICATION_HOME_RESOURCE_ID = %s and NOTIFICATION_UID = %s
                returning MODIFIED
                """,
                [self._xmlType.toxml(), xmldata, self._md5, self._home._resourceID, uid])
            self._modified = rows[0][0]
        
        self._objectText = xmldata


    @inlineCallbacks
    def _fieldQuery(self, field):
        data = yield self._txn.execSQL(
            "select " + field + " from NOTIFICATION "
            "where RESOURCE_ID = %s",
            [self._resourceID]
        )
        returnValue(data[0][0])


    @inlineCallbacks
    def xmldata(self):
        
        if self._objectText is None:
            self._objectText = (yield self._fieldQuery("XML_DATA"))
        returnValue(self._objectText)


    def contentType(self):
        """
        The content type of NotificationObjects is text/xml.
        """
        return MimeType.fromString("text/xml")


    def md5(self):
        return self._md5


    def size(self):
        return self._size

    def xmlType(self):
        # NB This is the NotificationType property element
        if isinstance(self._xmlType, str):
            # Convert into NotificationType property element
            self._xmlType = WebDAVDocument.fromString(self._xmlType).root_element

        return self._xmlType

    def created(self):
        return datetimeMktime(parseSQLTimestamp(self._created))


    def modified(self):
        return datetimeMktime(parseSQLTimestamp(self._modified))



