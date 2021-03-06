#!/usr/bin/env python

# Direct Requirements:
#  Twisted
#  PyOpenSSL
#  service_identity

import csv
import json
from datetime import date
from datetime import datetime
from datetime import timedelta
from functools import partial
from twisted.python import log
from twisted.internet import defer
from twisted.internet import reactor
from twisted.web.client import Agent
from twisted.web.client import readBody
from twisted.web.http_headers import Headers

TRAC_BUILDBOT_URL = 'http://trac.buildbot.net'
TRAC_BUILDBOT_TICKET_URL = TRAC_BUILDBOT_URL + '/ticket/%(ticket)s'
GITHUB_API_URL = 'https://api.github.com'
HTTP_HEADERS = Headers({'User-Agent': ['buildbot.net weekly summary']})

def get_body(what, f):
    def cb(resp):
        d = readBody(resp)
        d.addCallback(partial(f, what))
        return d
    return cb

def tablify_dict(d, show_header=True, field_formatter=None, row_order=None, col_order=None, col_padding=1):
    # Allow custom formatting of the fields. Default to right-justifying
    # everything but the "''" (i.e. first) column.
    if field_formatter is None:
        format_cell = lambda c, size, header: c.rjust(size) if header else c.ljust(size)
    else:
        format_cell = field_formatter
    # Allow the custom formatter to return None for a field. Add a function
    # that will iterate over each row to filter out the None cells before
    # joining. 
    skip_nones = lambda c: c is not None
    if row_order is None:
        rows = sorted(d.keys())
    else:
        rows = row_order
    # All values of the dict should have the same keys.
    if col_order is None:
        cols = sorted(d[rows[0]].keys())
    else:
        cols = col_order

    # At a minimum, need to be able to fit the column headers. The final value
    # is for the row names. Putting it at the end to keep subsequent enumerate
    # calls simple.
    col_widths = [len(c) for c in cols] + [0]
    for r in rows:
        col_widths[-1] = max(col_widths[-1], len(str(r)))
        for i, c in enumerate(cols):
            col_widths[i] = max(col_widths[i], len(str(d[r][c])))
    padding = ' ' * col_padding
    # The first row of the table is the header.
    if show_header:
        th_row = ([format_cell('', col_widths[-1], '')] + 
            [format_cell(c, col_widths[i], c) for i, c in enumerate(cols)])
        th = padding.join(filter(skip_nones, th_row))
        table = [th]
    else:
        table = []
    for r in rows:
        tr = [format_cell(r, col_widths[-1], '')]
        for i, c in enumerate(cols):
            value = d[r][c]
            tr.append(format_cell(str(value), col_widths[i], c))
        table.append(padding.join(filter(skip_nones, tr)))
    return '\n'.join(table)

def get_trac_tickets(start_day, end_day):
    """
    Get the last week's worth of tickets, where week ends through yesterday.
    """
    def format_trac_tickets(what, body):
        tickets = csv.reader(body.splitlines(), delimiter='\t')
        # Trac returns a tab-delimited file with the header. Skip it.
        next(tickets)
        # Returned format is id, summary, type.
        summary = [{'id': t[0], 'summary': t[1], 'type': t[2],
            'url': TRAC_BUILDBOT_TICKET_URL % {'ticket': t[0]}}
            for t in tickets]
        return (what, summary)

    def summarize_trac_tickets(results):
        col_padding = 2
        each_type = {'Opened': 0, 'Closed': 0}
        ticket_summary = {'Enhancements': each_type.copy(),
            'Defects': each_type.copy(), 'Tasks': each_type.copy(),
            'Regressions': each_type.copy(), 'Undecideds': each_type.copy(),
            'Other': each_type.copy(), 'Total': each_type.copy()}
        opened = {}
        closed = {}
        for success, value in results:
            if not success:
                continue
            what, tickets = value
            for t in tickets:
                Type = t['type'].capitalize() + 's'
                if Type in ticket_summary:
                    ticket_summary[Type][what] += 1
                else:
                    ticket_summary['Other'][what] += 1
                ticket_summary['Total'][what] += 1
                if what == 'Opened':
                    opened[len(opened)] = t
                elif what == 'Closed':
                    closed[len(closed)] = t
        # Convert ticket summary to a table to start the weekly summary.
        row_order = ['Enhancements', 'Defects', 'Regressions', 'Tasks',
            'Undecideds', 'Other', 'Total']
        col_order = ['Opened', 'Closed']
        ticket_table = tablify_dict(ticket_summary, row_order=row_order,
                col_order=col_order, col_padding=col_padding)
        ticket_overview = '\n'.join(['Ticket Summary', '-'*14, ticket_table])

        # Also include a list of every new/reopened and closed tickets.
        col_order = ['id', 'type', 'summary', 'url']
        # Left-justify every cell except the first column. Return None for the
        # first column to have it skipped.
        bug_list_formatter = lambda c, size, header: c.ljust(size) if header else None
        opened_table = tablify_dict(opened, show_header=False,
            col_order=col_order, col_padding=col_padding,
            field_formatter=bug_list_formatter)
        opened_overview = '\n'.join(['New/Reopened Tickets', '-'*20,
            opened_table])
        closed_table = tablify_dict(closed, show_header=False,
            col_order=col_order, col_padding=col_padding,
            field_formatter=bug_list_formatter)
        closed_overview = '\n'.join(['Closed Tickets', '-'*14, closed_table])

        trac_summary = [ticket_overview, opened_overview, closed_overview]
        return ('trac', '\n\n'.join(trac_summary))

    trac_query_url = ('%(trac_url)s/query?%(status)s&format=tab'
        '&changetime=%(start)s..%(end)s'
        '&col=id&col=summary&col=type&col=status&order=id')
    url_options = {
        'trac_url': TRAC_BUILDBOT_URL,
        'start': start_day,
        'end': end_day,
    }

    agent = Agent(reactor)
    fetches = []
    # Need to make two queries: one to get the new/reopened tickets and a
    # second to get the closed tickets.
    url_options['status'] = 'status=new&status=reopened'
    new_url = trac_query_url % (url_options)
    d = agent.request('GET', new_url, HTTP_HEADERS)
    d.addCallback(get_body('Opened', format_trac_tickets))
    fetches.append(d)

    url_options['status'] = 'status=closed'
    closed_url = trac_query_url % (url_options)
    d = agent.request('GET', closed_url, HTTP_HEADERS)
    d.addCallback(get_body('Closed', format_trac_tickets))
    fetches.append(d)

    dl = defer.DeferredList(fetches, fireOnOneErrback=True, consumeErrors=True)
    dl.addCallback(summarize_trac_tickets)
    return dl

def get_github_prs(start_day, end_day):
    """
    Get the last week's worth of tickets, where week ends through yesterday.
    """
    def summarize_github_prs(what, body_json):
        # I don't know a good way to parse the time zone. Github returns
        # ISO8601 in UTC.
        gh_time_format = '%Y-%m-%dT%H:%M:%SZ'
        opened_prs = {}
        closed_prs = {}
        body = json.loads(body_json)
        categories = [
            ('Opened', 'open', 'created_at', opened_prs),
            ('Completed', 'closed', 'closed_at', closed_prs),
        ]
        for pr in body:
            for group in categories:
                _, state, when, pr_dict = group
                # Have to check if the when field is not None. The state is
                # 'closed' for merged and unmerged pull requests. The merged
                # tuple is first, so any pull request that is closed and has a
                # merged_at date will be added there before checking the
                # closed_at date.
                if pr['state'] == state and pr[when] is not None:
                    happened = datetime.strptime(pr[when], gh_time_format)
                    # If this pull request was created outside of the summary
                    # period, skip it.
                    if happened.date() < start_day or happened.date() > end_day:
                        continue
                    pr_dict[len(pr_dict)] = pr

        overviews = []
        bug_list_formatter = lambda c, size, header: c.ljust(size) if header else None
        for group in categories:
            what, _, _, pr_dict = group
            title = what + ' Pull Requests'
            title_h2 = '-'*len(title)
            table = tablify_dict(pr_dict, show_header=False,
                row_order=sorted(pr_dict.keys(), lambda a,b: cmp(b, a)),
                col_order=['number', 'title', 'html_url'],
                col_padding=2, field_formatter=bug_list_formatter)
            overviews.append('\n'.join([title, title_h2, table]))
        return ('github', '\n\n'.join(overviews))


    gh_api_url = ('%(api_url)s/repos/buildbot/buildbot/pulls?state=all')
    url_options = {'api_url': GITHUB_API_URL}
    url = gh_api_url % (url_options)
    agent = Agent(reactor)
    d = agent.request('GET', url, HTTP_HEADERS)
    d.addCallback(get_body('Github', summarize_github_prs))
    return d


def summary(results):
    message = (
        "Trac Tickets\n"
        "============\n"
        "%(trac)s\n"
        "\n\n"
        "GitHub Pull Requests\n"
        "====================\n"
        "%(github)s")
    message_parts = {}
    for success, value in results:
        if not success:
            continue
        part, msg = value
        message_parts[part] = msg
    print message % message_parts

def main():
    end_day = date.today() - timedelta(1)
    start_day = end_day - timedelta(6)

    #dl = defer.DeferredList([get_trac_tickets()])
    dl = defer.DeferredList([
        get_trac_tickets(start_day, end_day),
        get_github_prs(start_day, end_day),
    ], fireOnOneErrback=True, consumeErrors=True)
    dl.addCallback(summary)
    dl.addErrback(log.err)
    dl.addCallback(lambda _: reactor.stop())
    reactor.run()


if __name__ == '__main__':
    main()
