import unittest
import os

import datetime
from pony import orm
from pony.orm import db_session

from augur import db, AugurContext, const
from augur import api

from augur import settings
from augur.integrations.augurgithub import AugurGithub
from augur.integrations.augurjira import AugurJira


class TestApi(unittest.TestCase):
    def setUp(self):

        os.environ['DB_TYPE'] = 'sqlite'
        os.environ['SQLITE_PATH'] = os.path.join(settings.main.project.base_dir, "tests/db.sqlite")

        settings.load_settings()

        db.init_db()

        self.jira_data = {
            "board_id": 3,
            "issue_key": "ENG-1",
            "filter_id": 10100,
            "epic_key": "ENG-24"
        }

        self.staff_data = [
            {
                "first_name": "Karim",
                "last_name": "Shehadeh",
                "company": "Under Armour",
                "avatar_url": "",
                "role": "Manager",
                "email": "me@email.com",
                "rate": 0.0,
                "start_date": datetime.date(year=2015, month=3, day=8),
                "type": "FTE",
                "jira_username": "kshehadeh",
                "github_username": "kshehadeh",
                "status": "Active",
                "teams": []
            },
            {
                "first_name": "John",
                "last_name": "Doe",
                "company": "John Doe Industries",
                "avatar_url": "",
                "role": "Developer",
                "email": "him@email.com",
                "rate": 10.0,
                "start_date": datetime.date(year=2017, month=1, day=1),
                "type": "Consultant",
                "jira_username": "jdoe",
                "github_username": "jdoe",
                "status": "Active",
                "teams": []
            }
        ]
        self.initialize_db()

    @db_session
    def initialize_db(self):

        staff = []
        for s in self.staff_data:
            staff.append(db.Staff(**s))

        orm.commit()

        board = db.AgileBoard(jira_id=self.jira_data['board_id'])
        orm.commit()

        product = db.Product(name="Todo App", key="todo")
        orm.commit()

        team = db.Team(name="Team Test")
        team.members.add(staff)
        team.agile_board = board
        team.product = product
        orm.commit()

        dev_project = db.ToolProject(tool_project_key="ENG")
        qa_project = db.ToolProject(tool_project_key="BUG")

        project_category = db.ToolProjectCategory(tool_category_name="Engineering Projects")

        statuses = []
        resolutions = []
        issuetypes = []
        if orm.select(tir for tir in db.ToolIssueResolution).count() == 0:
            for tir in (("fixed", "positive"), ("done", "positive"), ("complete", "positive"),
                        ("won't do", "negative"), ("duplicate", "negative"), ("not an issue", "negative")):
                resolutions.append(
                    db.ToolIssueResolution(tool_issue_resolution_name=tir[0], tool_issue_resolution_type=tir[1]))

        if orm.select(tis for tis in db.ToolIssueStatus).count() == 0:
            for tis in (("open", "open"), ("blocked", "in progress"), ("quality review", "in progress"),
                        ("staging", "in progress"), ("production", "in progress"), ("resolved", "done")):
                statuses.append(db.ToolIssueStatus(tool_issue_status_name=tis[0], tool_issue_status_type=tis[1]))

        if orm.select(tit for tit in db.ToolIssueType).count() == 0:
            issue_types_type = {
                "story": "story",
                "bug": "bug",
                "task": "task",
                "sub-task": "task",
                "defect": "bug"
            }
            for tit, tit_type in issue_types_type.iteritems():
                issuetypes.append(db.ToolIssueType(tool_issue_type_name=tit, tool_issue_type_type=tit_type))

        defect_filter = db.WorkflowDefectProjectFilter(project_key="BUG")
        defect_filter.issue_types.add(orm.get(it for it in db.ToolIssueType if it.tool_issue_type_name == "bug"))

        orm.commit()

        flow = db.Workflow(name="Test Workflow")
        flow.statuses.add(statuses)
        flow.resolutions.add(resolutions)
        flow.projects.add([dev_project, qa_project])
        flow.categories.add(project_category)
        flow.defect_projects.add(defect_filter)
        flow.issue_types.add(issuetypes)

        orm.commit()
        self.workflow_id = flow.id

        group = db.Group(name="Test Group", workflow=flow, products=[product], teams=[team])

        orm.commit()

        self.group_id = group.id

    def tearDown(self):
        if self.sqlite_db_path:
            os.remove(self.sqlite_db_path)

    @db_session
    def runTest(self):
        self.context()
        self.get_github()
        self.get_jira()
        self.get_workflow()
        self.get_group()
        self.get_groups()
        self.get_abridged_team_sprint()
        self.get_sprint_info_for_team()
        self.get_issue_details()
        self.get_defect_data()
        self.get_historical_defect_data()
        self.get_releases_since()
        self.get_epic_analysis()
        self.get_filter_analysis()
        self.get_user_worklog()
        self.get_dashboard_data()

    def context(self):
        context = AugurContext(self.group_id)
        api.set_default_context(context)
        context = api.get_default_context()
        self.assertIsNotNone(context)
        self.assertIsNotNone(context.group)
        self.assertIsNotNone(context.workflow)
        self.assertEqual(context.group.id, self.group_id)

    def get_github(self):
        gh = api.get_github()
        self.assertIsInstance(gh, AugurGithub)

    def get_jira(self):
        gh = api.get_jira()
        self.assertIsInstance(gh, AugurJira)

    def get_workflow(self):
        wf = api.get_workflow(self.workflow_id)
        self.assertIsInstance(wf, db.Workflow)

    def get_group(self):
        g = api.get_group(self.group_id)
        self.assertIsInstance(g, db.Group)

    def get_groups(self):
        groups = api.get_groups()
        self.assertEqual(len(groups), 1, "Expected exactly one group created so far")

    def get_abridged_team_sprint(self):
        sprints = api.get_abridged_sprint_list_for_team(1)
        self.assertIsNotNone(sprints)
        self.assertIsInstance(sprints, list, "Expected a list returned")
        self.assertEqual(len(sprints), 1, "Expected exactly one sprint returned")

    def get_sprint_info_for_team(self):
        sprint = api.get_sprint_info_for_team(1, sprint_id=const.SPRINT_CURRENT)
        self.assertIsInstance(sprint, dict)

    def get_issue_details(self):
        issue = api.get_issue_details(self.jira_data['issue_key'])
        self.assertIsInstance(issue, dict)

    def get_defect_data(self):
        data = api.get_defect_data(context=api.get_default_context())
        self.assertIsInstance(data, dict)
        self.assertIn('lookback_days', data)
        self.assertIn('current_period', data)

    def get_historical_defect_data(self):
        data = api.get_historical_defect_data(context=api.get_default_context())
        self.assertIsInstance(data, dict)
        self.assertIn('num_weeks', data)
        self.assertIn('weeks', data)

    def get_releases_since(self):
        # TODO: Currently releases are not context sensitive.
        pass

    def get_filter_analysis(self):
        data = api.get_filter_analysis(self.jira_data['filter_id'], context=api.get_default_context())
        self.assertIsInstance(data, dict)

    def get_epic_analysis(self):
        data = api.get_epic_analysis(self.jira_data['epic_key'], context=api.get_default_context())
        self.assertIsInstance(data, dict)

    def get_user_worklog(self):
        data = api.get_user_worklog(start="2017-08-07", end="2017-08-07", team_id=1)
        self.assertIsInstance(data, dict)

    def get_dashboard_data(self):
        data = api.get_dashboard_data(context=api.get_default_context())
        self.assertIsInstance(data, dict)