import datetime

import dateutil
import pymongo
import pytz

from augur.common.timer import Timer
import augur.common.serializers
import augur.settings

import augur.signals

from dateutil.parser import parse


class UaNoDataException(Exception):
    pass


class UaStatsDb(pymongo.MongoClient):
    def __init__(self,
                 host=augur.settings.main.datastores.cache.mongo.host,
                 port=augur.settings.main.datastores.cache.mongo.port,
                 document_class=dict,
                 tz_aware=False,
                 connect=True,
                 **kwargs):
        super(UaStatsDb, self).__init__(host, port, document_class, tz_aware, connect, **kwargs)


class UaModel(object):
    """
    Handles loading and saving of data in a consistent way including database agnostic unique
    keys and ttl.
    """

    def __init__(self, mongo_client):
        self.mongo_client = mongo_client
        self.data = []

        if self.get_unique_key():
            self.get_collection().create_index([(self.get_unique_key(), pymongo.ASCENDING)], unique=True)

    def get_unique_key(self):
        """
        If you want to ensure that we don't add a duplicate to the collection return
        the name of the field that will act as the unique key. By default there is no unique index.
        :return:
        """
        return None

    def get_ttl(self):
        """
        If the returned data is older than x seconds then return an empty
        response.
        :return: ttl as timedelta object
        """
        return None

    def empty(self):
        """
        Removes all data from the mongo collection
        :return:
        """
        self.get_collection().delete_many({})

    def get_unique_type(self):
        """
        By default, we don't return anything for the type and expect that everything in the collection
        associated with this model is the same.  Use the type when you have a single collection that contains
        multiple types of data (for use in caching result sets that may not require historical reference)
        :return:
        """
        return None

    def clear_before_add(self):
        return False

    def requires_transform(self):
        return True

    def get_collection(self):
        raise NotImplemented

    def clear_data(self):
        self.data = list()

    def has_data(self):
        return self.get_collection().count() > 0

    def add_data(self, data):
        if not self.data:
            self.data = list()

        if isinstance(data, list):
            self.data.extend(data)
        else:
            self.data.append(data)

    def update(self, data=None):

        if self.get_unique_key() is None:
            raise ValueError("You cannot update a document in a collection that does not have a unique index")

        if data:
            # use the given data instead of stored data if given in call
            self.clear_data()
            self.add_data(data)

        if not self.data:
            raise ValueError("No data found to update")

        self.decorate_data()

        data = self.data if isinstance(self.data, list) else [self.data]

        unique_key = self.get_unique_key()
        updates = []
        for d in data:
            updates.append({unique_key: d[unique_key]})
            self.get_collection().update({
                unique_key: d[unique_key]},
                augur.common.serializers.to_mongo(d), upsert=True)

        augur.signals.cache_updated.send(sender=self.__class__, cache_name=self.get_collection().name, update_info=updates,
                                         key_count=len(data))

    def decorate_data(self):
        storage_type = self.get_unique_type()
        if isinstance(self.data, list):
            for d in self.data:
                d['storage_time'] = datetime.datetime.utcnow()
                d['storage_type'] = storage_type

        else:
            self.data['storage_time'] = datetime.datetime.utcnow()
            self.data['storage_type'] = storage_type

    def save(self, data=None):

        if data:
            # use the given data instead of stored data if given in call
            self.clear_data()
            self.add_data(data)

        if not self.data:
            raise ValueError("No data found to save")

        self.decorate_data()

        if self.clear_before_add():
            # empty the collection first if requested by model
            self.get_collection().remove()

        if self.get_unique_key():
            self.get_collection().create_index([(self.get_unique_key(), pymongo.DESCENDING)])

        if type(self.data) is not list:
            self.data = [self.data]

        if self.get_unique_key():
            updates = []
            unique_key = self.get_unique_key()
            for d in self.data:
                updates.append({"id": d[unique_key]})
                self.get_collection().find_one_and_replace({"id": d[unique_key]}, augur.common.serializers.to_mongo(d), upsert=True)

            augur.signals.cache_updated.send(sender=self.__class__, cache_name=self.get_collection().name,
                                             update_info=updates, key_count=len(self.data))
            success = True
        else:
            result = self.get_collection().insert_many([augur.common.serializers.to_mongo(d) for d in self.data])
            augur.signals.cache_updated.send(sender=self.__class__, cache_name=self.get_collection().name, update_info=None,
                                             key_count=len(result.inserted_ids))
            success = True

        return success

    def load(self, query_object=None, limit=None, order_by=None, sort_order=pymongo.DESCENDING, override_ttl=None):

        with Timer("UaModel '%s' load" % type(self).__name__) as t:

            self.clear_data()

            if not override_ttl:
                ttl = self.get_ttl()
            else:
                ttl = override_ttl

            if not query_object:
                query_object = {}

            # append the storage_time ttl check if one is given either as an override or as
            # part of the model.
            if ttl and 'storage_time' not in query_object:
                query_object.update({
                    'storage_time': {"$gte": datetime.datetime.utcnow() - ttl},
                })

            if self.get_unique_type():
                query_object.update({
                    'storage_type': self.get_unique_type()
                })

            t.split("Finished preparing query object")

            # now make the initial query
            cursor = self.get_collection().find(query_object)

            # and add any necessary clauses.
            if order_by:
                cursor.sort(order_by, sort_order)

            if limit:
                cursor.limit(1)

            t.split("Finished preparing cursor")

            # turn the cursor results into an array suitable for return
            for result in cursor:
                self.data.append(result)

            t.split("Finished iterating over cursor")

        if self.data and len(self.data) > 0:
            # get the storage time of the first item to pass along with the signal for audit purposes.
            storage_time = None
            if 'storage_time' in self.data[0]:
                storage_time = self.data[0]['storage_time']

            augur.signals.cache_item_loaded.send(sender=self.__class__,
                                                 ttl=ttl,
                                                 query_object=query_object,
                                                 cache_name=self.get_collection().name,
                                                 cache_date=storage_time,
                                                 key_count=len(self.data))

        return self.data


class UaDashboardData(UaModel):
    def clear_before_add(self):
        return True

    def get_ttl(self):
        return datetime.timedelta(hours=3)

    def requires_transform(self):
        return True

    def get_collection(self):
        return self.mongo_client.stats.dashboard

    def load_dashboard(self):
        results = self.load(limit=1, order_by='storage_time', sort_order=pymongo.DESCENDING)
        if len(results) > 0:
            return results[0]
        return None


class UaAllTeamsData(UaModel):
    def get_ttl(self):
        return datetime.timedelta(hours=2)

    def clear_before_add(self):
        return True

    def requires_transform(self):
        return True

    def get_collection(self):
        return self.mongo_client.stats.developers


class UaTeamSprintData(UaModel):
    def clear_before_add(self):
        return False

    def requires_transform(self):
        return True

    def get_unique_key(self):
        return 'sprint_id'

    def get_collection(self):
        return self.mongo_client.stats.teamstats

    def load_sprint(self, sprint_id, override_ttl=None):
        results = self.load({
            'sprint_id': sprint_id
        }, override_ttl=override_ttl)

        if len(results) > 0:
            return results[0]
        return None


class UaPermissionsOrgData(UaModel):
    """
    Stores users who have extended permissions to view data on feature dev site.
    """

    def clear_before_add(self):
        return True

    def get_collection(self):
        return self.mongo_client.stats.permissions

    def requires_transform(self):
        return True

    def get_user(self, username):
        result = self.load({"login": username})
        if len(result) > 0:
            return result[0]
        else:
            return None


class UaTeamOpenPullRequestsData(UaModel):
    """
    Store open PRs.  Clears the cache with each update.
    """

    def clear_before_add(self):
        return False

    def get_collection(self):
        return self.mongo_client.stats.open_pulls

    def requires_transform(self):
        return True

    def get_unique_key(self):
        return "id"

    def load_open_prs(self, by_user=None):

        if by_user:
            query = {
                'user.login': str(by_user),
            }
        else:
            query = None

        return self.load(query)

    def save_prs(self, data):
        for d in data:
            d['merged_at'] = parse(d['merged_at']) if d['merged_at'] else None
            d['created_at'] = parse(d['created_at']) if d['created_at'] else None

        super(UaTeamOpenPullRequestsData, self).save(data)


class UaTeamPullRequestsData(UaModel):
    """
    Stores PRs across the
    """

    def clear_before_add(self):
        return False

    def get_collection(self):
        return self.mongo_client.stats.pulls

    def requires_transform(self):
        return True

    def get_unique_key(self):
        return "id"

    def load_prs_since(self, since):

        if since:
            query = {
                'merged_at': {"$gte": since},
            }
        else:
            query = None

        return self.load(query)

    def get_most_recent_pr(self):

        # now make the initial query
        cursor = self.get_collection().find()
        cursor.sort('created_at', pymongo.DESCENDING)
        cursor.limit(1)
        return cursor.next() if cursor.count() > 0 else None

    def save_prs(self, data):
        for d in data:
            d['merged_at'] = dateutil.parser.parse(d['merged_at']) if d['merged_at'] else None
            d['created_at'] = dateutil.parser.parse(d['created_at']) if d['created_at'] else None

        super(UaTeamPullRequestsData, self).save(data)


class UaDeveloperData(UaModel):
    def get_ttl(self):
        return datetime.timedelta(hours=1)

    def clear_before_add(self):
        return False

    def get_collection(self):
        return self.mongo_client.stats.devdetails

    def requires_transform(self):
        return True

    def load_user(self, username, look_back_days):
        user_array = self.load({
            'username': username,
            'num_days': look_back_days
        })

        if len(user_array) > 0:
            return user_array[0]
        else:
            return None


class UaEngineeringReportData(UaModel):
    def get_ttl(self):
        return datetime.timedelta(hours=24)

    def clear_before_add(self):
        return False

    def get_collection(self):
        return self.mongo_client.stats.engineering_report

    def requires_transform(self):
        return True

    def load_data(self, week_number):
        results = self.load({
            'week_number': week_number
        })

        if len(results) > 0:
            return results[0]
        else:
            return None


class UaJiraIssueData(UaModel):
    def get_ttl(self):
        return datetime.timedelta(hours=1)

    def clear_before_add(self):
        return False

    def get_collection(self):
        return self.mongo_client.stats.jira_issues

    def requires_transform(self):
        return True

    def load_issue(self, issue_key):
        issues = self.load({
            'key': issue_key
        })

        if len(issues) > 0:
            return issues[0]
        else:
            return None


class UaJiraWorklogData(UaModel):
    def get_ttl(self):
        return datetime.timedelta(hours=8)

    def clear_before_add(self):
        return False

    def get_collection(self):
        return self.mongo_client.stats.worklogs

    def requires_transform(self):
        return True

    def load_worklog(self, start, end, team_id, username=None, project=None):

        if not start or not end or not team_id:
            raise LookupError("You must specify start, end and team_id at least")

        query = {
            'start': start.datetime,
            'end': end.datetime,
            'team_id': team_id
        }

        if project:
            query['project'] = project

        if username:
            query['username'] = username

        worklog_data = self.load(query)

        if len(worklog_data) > 0:
            return worklog_data[0]
        else:
            return None


class UaJiraDefectData(UaModel):
    def get_ttl(self):
        return datetime.timedelta(hours=2)

    def clear_before_add(self):
        return False

    def get_collection(self):
        return self.mongo_client.stats.defects

    def requires_transform(self):
        return True

    def load_defects(self, lookback_days):
        defect_data = self.load({
            'lookback_days': lookback_days
        })

        if len(defect_data) > 0:
            return defect_data[0]
        else:
            return None


class UaJiraDefectHistoryData(UaModel):
    def get_ttl(self):
        return datetime.timedelta(hours=2)

    def clear_before_add(self):
        return False

    def get_collection(self):
        return self.mongo_client.stats.defect_history

    def requires_transform(self):
        return True

    def load_defects(self, num_weeks):
        defect_data = self.load({
            'num_weeks': num_weeks
        })

        if len(defect_data) > 0:
            return defect_data[0]
        else:
            return None


class RecentEpicData(UaModel):
    def get_ttl(self):
        return datetime.timedelta(hours=24)

    def clear_before_add(self):
        return False

    def get_collection(self):
        return self.mongo_client.stats.recent_epics

    def requires_transform(self):
        return True

    def load_recent_epics(self):
        result = self.load()
        if isinstance(result, list) and len(result) > 0:
            return result[0]
        else:
            return result


class UaJiraEpicData(UaModel):
    def get_ttl(self):
        return datetime.timedelta(hours=8)

    def clear_before_add(self):
        return False

    def get_collection(self):
        return self.mongo_client.stats.epics

    def requires_transform(self):
        return True

    def load_epic(self, epic_key):
        epics = self.load({
            'epic.key': epic_key
        })

        if len(epics) > 0:
            return epics[0]
        else:
            return None


class UaJiraOrgData(UaModel):
    def get_ttl(self):
        return datetime.timedelta(hours=24)

    def clear_before_add(self):
        return True

    def get_collection(self):
        return self.mongo_client.stats.org

    def requires_transform(self):
        return True


class UaJiraFilterData(UaModel):
    def get_ttl(self):
        return datetime.timedelta(hours=2)

    def clear_before_add(self):
        return False

    def get_collection(self):
        return self.mongo_client.stats.filters

    def requires_transform(self):
        return True

    def load_filter(self, filer_id):
        filter_ob = self.load({
            'filter.id': str(filer_id)
        })

        if len(filter_ob) > 0:
            return filter_ob[0]
        else:
            return None


class UaJiraSprintsData(UaModel):
    def get_unique_key(self):
        return 'id'

    def get_ttl(self):
        return datetime.timedelta(hours=2)

    def clear_before_add(self):
        return False

    def get_collection(self):
        return self.mongo_client.stats.sprints

    def requires_transform(self):
        return True

    def load_team_sprints(self, team_id):
        return self.load({
            'team_id': team_id
        }, order_by='endDate', sort_order=pymongo.DESCENDING)


class UaCachedResultSets(UaModel):
    def get_ttl(self):
        return datetime.timedelta(hours=2)

    def clear_before_add(self):
        return False

    def get_collection(self):
        return self.mongo_client.stats.result_cache

    def requires_transform(self):
        return True

    def load_from_key(self, key, override_ttl=None):
        return self.load(query_object={
            'key': key
        }, override_ttl=override_ttl)

    def save_with_key(self,data, key):

        # first, remove anything with this key
        self.mongo_client.stats.result_cache.remove({'key': key})

        data['key'] = key
        super(UaCachedResultSets, self).save(data)


class UaProductReportData(UaModel):
    def __init__(self, mongo_client):
        super(UaProductReportData, self).__init__(mongo_client)

    def get_ttl(self):
        return datetime.timedelta(hours=2)

    def clear_before_add(self):
        return True

    def get_collection(self):
        return self.mongo_client.stats.product_report

    def requires_transform(self):
        return True


class UaReleaseData(UaModel):
    """
    Used to store information about releases
    """

    def get_ttl(self):
        return datetime.timedelta(hours=8)

    def clear_before_add(self):
        return False

    def requires_transform(self):
        return True

    def get_collection(self):
        return self.mongo_client.stats.release

    def load_release_data(self, start, end):
        release_data = self.load({
            'release_date_start': start,
            'release_date_end': end
        })

        if len(release_data) > 0:
            return release_data[0]
        else:
            return None


class UaTempoTeamData(UaModel):
    """

    """

    def get_ttl(self):
        return datetime.timedelta(hours=168)

    def clear_before_add(self):
        return False

    def requires_transform(self):
        return True

    def get_collection(self):
        return self.mongo_client.stats.release

    def load_team_data(self, team_id):
        data = self.load({
            'team_id': team_id
        })

        if len(data) > 0:
            return data[0]
        else:
            return None


class UaComponentOwnership(UaModel):
    """
    Used to store component owners and maintainers as retrieved from github
    """

    def __init__(self, mongo_client):
        super(UaComponentOwnership, self).__init__(mongo_client)

    def get_ttl(self):
        return datetime.timedelta(hours=12)

    def clear_before_add(self):
        return False

    def get_collection(self):
        return self.mongo_client.stats.component_owners

    def requires_transform(self):
        return False

    def load_org(self, org):
        data = self.load({
            'org': org
        })

        if len(data) > 0:
            return data[0]
        else:
            return None


class UaGithubDevStats(object):
    def __init__(self, username):
        self.user = username
        self.prs = []
        self.avg_changed_files_per_pr = 0
        self.avg_comments_per_pr = 0
        self.highest_changes_in_pr = 0
        self.highest_comments_in_pr = 0
        self.avg_length_of_time_pr_was_open = datetime.timedelta()
        self.dirty = False

    def reset(self):
        self.__init__(self.user)

    def add_pr(self, pr):

        if pr['user']['login'] == self.user and pr['merged']:
            self.dirty = True

            # we need a version of this attribute that doesn't have the underscore so that we can
            #   display its values in a django template.
            pr['links'] = pr['_links']
            self.prs.append(pr)

            comment_count = (pr['review_comments'] + pr['comments'])
            self.avg_changed_files_per_pr += pr['changed_files']
            self.avg_comments_per_pr += comment_count
            if pr['changed_files'] > self.highest_changes_in_pr:
                self.highest_changes_in_pr = pr['changed_files']
            if comment_count > self.highest_comments_in_pr:
                self.highest_comments_in_pr = comment_count

            merged_at = pr['merged_at']
            created_at = pr['created_at']

            if not isinstance(pr['merged_at'], datetime.datetime):
                merged_at = dateutil.parser.parse(pr['merged_at'])
            merged_at = merged_at.replace(tzinfo=None)

            if not isinstance(pr['created_at'], datetime.datetime):
                created_at = dateutil.parser.parse(pr['created_at']).replace(tzinfo=pytz.UTC)
            created_at = created_at.replace(tzinfo=None)

            self.avg_length_of_time_pr_was_open += (merged_at - created_at)

    def as_dict(self):

        if self.dirty:
            self.avg_changed_files_per_pr /= len(self.prs)
            self.avg_comments_per_pr /= len(self.prs)

            avg_secs = self.avg_length_of_time_pr_was_open.total_seconds() / len(self.prs)
            self.avg_length_of_time_pr_was_open = datetime.timedelta(seconds=avg_secs)
            self.dirty = False

        return {
            'prs': self.prs,
            'avg_changed_files_per_pr': self.avg_changed_files_per_pr,
            'avg_comments_per_pr': self.avg_comments_per_pr,
            'highest_changes_in_pr': self.highest_changes_in_pr,
            'highest_comments_in_pr': self.highest_comments_in_pr,
            'avg_length_of_time_pr_was_open': self.avg_length_of_time_pr_was_open,
        }