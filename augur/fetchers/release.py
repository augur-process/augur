import arrow

from augur.common import cache_store
from augur.fetchers.fetcher import UaDataFetcher


class UaRelease(UaDataFetcher):
    """
    Retrieves analyzed data returned from a filter that has been already created in Jira
    """

    def __init__(self, uajira, force_update=False):
        super(UaRelease, self).__init__(uajira, force_update)
        self.start = None
        self.end = None

    def init_cache(self):
        self.cache = cache_store.UaReleaseData(self.uajira.mongo)

    def cache_data(self, data):
        self.recent_data = data
        self.cache.save(self.recent_data)
        return self.recent_data

    def get_cached_data(self):
        self.recent_data = self.cache.load_release_data(start=self.start.datetime, end=self.end.datetime)

        # when retrieving from cache, we get a list back by default.  we don't want that.
        if isinstance(self.recent_data, list) and len(self.recent_data) > 0:
            self.recent_data = self.recent_data[0]

        return self.recent_data

    def validate_input(self, **args):
        if 'start' not in args or 'end' not in args:
            raise LookupError("You must specify a start and end date for releases")
        else:
            self.start = arrow.get(args['start']).replace(tzinfo=None)
            self.end = arrow.get(args['end']).replace(tzinfo=None)

        return True

    def _fetch(self):

        startStr = self.start.format("YYYY/MM/DD HH:mm")
        endStr = self.end.format("YYYY/MM/DD HH:mm")

        issues = self.uajira.execute_jql("project in (CM) AND (status changed "
                                         "to \"Production Deployed\" during ('%s','%s'))" % (startStr, endStr))

        released_tickets = {}

        for issue in issues:
            links = issue.fields.issuelinks

            if isinstance(links, list) and len(links) > 0:
                for link in links:
                    if int(link.type.id) == 10653:

                        # append the associated cm to the ticket.
                        linkedIssue = None
                        if hasattr(link, 'outwardIssue'):
                            linkedIssue = link.outwardIssue
                        elif hasattr(link, 'inwardIssue'):
                            linkedIssue = link.inwardIssue

                        if linkedIssue:
                            rawLinkedIssue = linkedIssue.raw
                            rawLinkedIssue['cm'] = issue.key
                            released_tickets[linkedIssue.key] = rawLinkedIssue

        # get complete issue info
        fully_released_tickets = []
        if len(released_tickets) > 0:
            comma_separated_keys = ",".join(released_tickets.keys())
            fully_released_tickets = self.uajira.execute_jql(
                "key in (%s) order by \"Dev Team\" asc, key asc" % (comma_separated_keys))
        final_ticket_list = []
        for t in fully_released_tickets:
            final_ob = released_tickets[t.key]
            final_ob['detail'] = t.raw
            final_ticket_list.append(final_ob)

        return self.cache_data({
            'release_date_start': self.start.datetime,
            'release_date_end': self.end.datetime,
            'issues': final_ticket_list
        })