try:
    from xml.etree import cElementTree as ET
except ImportError as e:
    from xml.etree import ElementTree as ET

import redis
import pandas.io.sql as pdsql
import pprint
import pymysql
import pickle

from GSVScraper import *
from time import sleep
from pandas import Series, DataFrame

pp = pprint.PrettyPrinter(indent=2)


_sidewalk_db_cache = {}

class RecordError(Exception):
    def __init__(self, value):
        self.value = value

    def __str__(self):
        return repr(self.value)
    
    
class XMLAcquisitionError(Exception):
    def __init__(self, value):
        self.value = value

    def __str__(self):
        return repr(self.value) 
    
    
class SidewalkDB(object):
    def __init__(self, database='sidewalk', use_sscursor=False, **kwargs):
        self._database = database

        if "host" in kwargs:
            self._host = kwargs["host"]
        else:
            self._host = '127.0.0.1'

        if "port" in kwargs:
            self._port = kwargs["port"]
        else:
            self._port = 3306

        if "user" in kwargs:
            self._user = kwargs["user"]
        else:
            self._user = 'root'

        if "password" in kwargs:
            self._password = kwargs["password"]
        else:
            self._password = ''
        if use_sscursor:
            self._conn = pymysql.connect(host=self._host, port=self._port, user=self._user, passwd=self._password,
                                         db=self._database, cursorclass=pymysql.SSCursor)
        else:
            self._conn = pymysql.connect(host=self._host, port=self._port, user=self._user, passwd=self._password,
                                         db=self._database)
        self._cur = self._conn.cursor()

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        if type is None:
            self._cur.connection.commit()
        else:
            self._cur.connection.rollback()

        self._cur.close()
        self._conn.close()

    def fetch_city(self, panorama_id, return_dataframe=False):
        sql = """SELECT * FROM Intersections
WHERE NearestGSVPanoramaId = %s"""
        return self.query(sql, (panorama_id,), return_dataframe=return_dataframe)

    def fetch_label_points(self, task_description='VerificationExperiment_2', return_dataframe=True, **kwargs):
        """
        This method returns label points
        """
        sql = """SELECT Labels.LabelGSVPanoramaId, Labels.LabelTypeId, Labels.Deleted,
        LabelPoints.LabelId, LabelPoints.svImageX, LabelPoints.svImageY
        FROM LabelPoints
INNER JOIN Labels
ON Labels.LabelId = LabelPoints.LabelId
INNER JOIN LabelingTasks
ON LabelingTasks.LabelingTaskId = Labels.LabelingTaskId
INNER JOIN TaskPanoramas
ON TaskPanoramas.TaskPanoramaId = LabelingTasks.TaskPanoramaId
WHERE TaskPanoramas.TaskDescription = %s """

        if 'only_original' in kwargs and kwargs['only_original']:
            sql += " AND LabelingTasks.PreviousLabelingTaskId IS NULL"

        return self.query(sql, (task_description, ), return_dataframe=return_dataframe)

    def fetch_golden_label_points(self, label_type_id, return_dataframe=True):
        """
        This method retrieves the golden label points
        """
        sql = """SELECT LabelPoints.LabelId, svImageX, svImageY, Labels.LabelGSVPanoramaId, Assignments.*,
        LabelingTasks.PreviousLabelingTaskId, LabelingTasks.LabelingTaskId
FROM Assignments
INNER JOIN LabelingTasks
ON LabelingTasks.AssignmentId = Assignments.AssignmentId
INNER JOIN TaskPanoramas
ON TaskPanoramas.TaskGSVPanoramaId = LabelingTasks.TaskGSVPanoramaId
INNER JOIN Labels
ON Labels.LabelingTaskId = LabelingTasks.LabelingTaskId
INNER JOIN LabelPoints
ON LabelPoints.LabelId = Labels.LabelId
WHERE
Assignments.AmazonTurkerId = 'Researcher_Kotaro' AND
TaskPanoramas.TaskDescription = 'GoldenInsertion' AND
Labels.Deleted = 0 AND
Labels.LabelTypeId = %s"""
        df = self.query(sql, (str(label_type_id)), return_dataframe=return_dataframe)
        previous_task_ids = df.PreviousLabelingTaskId[~df.PreviousLabelingTaskId.isnull()].unique()
        df = df[~df.LabelingTaskId.isin(previous_task_ids)]
        return df

    def fetch_labeling_tasks(self, task_description, panorama_id=None, assignment_description=None, workers=None,
                             return_dataframe=False):
        sql = """
        SELECT Assignments.AmazonTurkerId, Assignments.TaskDescription AS AssignmentDescription,
        LabelingTasks.LabelingTaskId, LabelingTasks.AssignmentId, LabelingTasks.TaskPanoramaId,
        LabelingTasks.TaskGSVPanoramaId AS GSVPanoramaId, LabelingTasks.NoLabel, LabelingTasks.Description,
        LabelingTasks.PreviousLabelingTaskId, TaskPanoramas.TaskDescription, Intersections.City
        FROM Assignments
        INNER JOIN LabelingTasks
        ON LabelingTasks.AssignmentId = Assignments.AssignmentId
        INNER JOIN TaskPanoramas
        ON TaskPanoramas.TaskPanoramaId = LabelingTasks.TaskPanoramaId
        INNER JOIN Intersections
        ON Intersections.NearestGSVPanoramaId = LabelingTasks.TaskGSVPanoramaId
        WHERE TaskPanoramas.TaskDescription = %s
        AND Assignments.Completed = 1
        """

        if panorama_id is not None:
            sql += " AND TaskPanoramas.TaskGSVPanoramaId = '%s'" % panorama_id

        if assignment_description is not None:
            sql += " AND Assignments.TaskDescription = '%s'" % assignment_description

        if workers is not None:
            workers = ["'%s'" % worker for worker in workers]
            sql += " AND (Assignments.AmazonTurkerId = " + \
                   " OR Assignments.AmazonTurkerId = ".join(workers) + ")"

        return self.query(sql, (task_description,), return_dataframe=return_dataframe)

    def fetch_labeling_tasks_with_task_ids(self, task_ids, return_dataframe=True):
        sql = """
        SELECT Assignments.AmazonTurkerId, Assignments.TaskDescription AS AssignmentDescription,
        LabelingTasks.LabelingTaskId, LabelingTasks.AssignmentId, LabelingTasks.TaskPanoramaId,
        LabelingTasks.TaskGSVPanoramaId AS GSVPanoramaId, LabelingTasks.NoLabel, LabelingTasks.Description,
        LabelingTasks.PreviousLabelingTaskId, TaskPanoramas.TaskDescription, Intersections.City
        FROM Assignments
        INNER JOIN LabelingTasks
        ON LabelingTasks.AssignmentId = Assignments.AssignmentId
        INNER JOIN TaskPanoramas
        ON TaskPanoramas.TaskPanoramaId = LabelingTasks.TaskPanoramaId
        INNER JOIN Intersections
        ON Intersections.NearestGSVPanoramaId = LabelingTasks.TaskGSVPanoramaId
        WHERE Assignments.Completed = 1
        """
        task_ids = map(str, task_ids)
        sql += " AND (LabelingTasks.LabelingTaskId = " + \
                " OR LabelingTasks.LabelingTaskId = ".join(task_ids) + ")"

        return self.query(sql, return_dataframe=return_dataframe)

    def fetch_labels(self, label_type_id=1, workers=["Researcher_Kotaro"], return_dataframe=True):
        sql = """
        SELECT Labels.*, LabelingTasks.PreviousLabelingTaskId, Intersections.City from Labels
        INNER JOIN LabelingTasks
        ON LabelingTasks.LabelingTaskId = Labels.LabelingTaskId
        INNER JOIN Assignments
        ON Assignments.AssignmentId = LabelingTasks.AssignmentId
        INNER JOIN Intersections
        ON Intersections.NearestGSVPanoramaId = LabelingTasks.TaskGSVPanoramaId
        """
        if workers is not None:
            workers = ["'%s'" % worker for worker in workers]
            sql += " WHERE (Assignments.AmazonTurkerId = " + \
                   " OR Assignments.AmazonTurkerId = ".join(workers) + ")"

        df = self.query(sql, return_dataframe=return_dataframe)
        df = df[df.Deleted != 1]
        previous_task_ids = df.PreviousLabelingTaskId[~df.PreviousLabelingTaskId.isnull()].unique()
        df = df[~df.LabelingTaskId.isin(previous_task_ids)]

        return df

    def fetch_task_label_points(self, labeling_task_id, return_dataframe=True, **kwargs):
        if 'task_description' in kwargs:
            sql = """SELECT LabelingTasks.LabelingTaskId, LabelingTasks.TaskGSVPanoramaId AS GSVPanoramaId, PanoYawDeg, LabelType,
Labels.LabelId, Labels.Deleted, LabelPoints.LabelPointId, svImageX, svImageY, heading, pitch, zoom, labelLat, labelLng
FROM LabelingTasks
INNER JOIN TaskPanoramas
ON TaskPanoramas.TaskPanoramaId = LabelingTasks.TaskPanoramaId
INNER JOIN PanoramaProjectionProperties
ON PanoramaProjectionProperties.GSVPanoramaId = LabelingTasks.TaskGSVPanoramaId
INNER JOIN Labels
ON Labels.LabelingTaskId = LabelingTasks.LabelingTaskId
INNER JOIN LabelTypes
ON Labels.LabelTypeId = LabelTypes.LabelTypeId
INNER JOIN LabelPoints
ON LabelPoints.LabelId = Labels.LabelId
WHERE TaskPanoramas.TaskDescription = %s"""

            global _sidewalk_db_cache
            key = 'SidewalkDB.fetch_task_label_points.%s' % kwargs['task_description']

            if key in _sidewalk_db_cache:
                df = _sidewalk_db_cache[key]
            else:
                df = self.query(sql, (kwargs['task_description'],), return_dataframe=True)
                _sidewalk_db_cache[key] = df
            return df[df.LabelingTaskId == labeling_task_id]
        else:
            sql = """SELECT LabelingTasks.LabelingTaskId, LabelingTasks.TaskGSVPanoramaId AS GSVPanoramaId, PanoYawDeg, LabelType,
Labels.LabelId, Labels.Deleted, LabelPoints.LabelPointId, svImageX, svImageY, heading, pitch, zoom, labelLat, labelLng
FROM LabelingTasks
INNER JOIN PanoramaProjectionProperties
ON PanoramaProjectionProperties.GSVPanoramaId = LabelingTasks.TaskGSVPanoramaId
INNER JOIN Labels
ON Labels.LabelingTaskId = LabelingTasks.LabelingTaskId
INNER JOIN LabelTypes
ON Labels.LabelTypeId = LabelTypes.LabelTypeId
INNER JOIN LabelPoints
ON LabelPoints.LabelId = Labels.LabelId
WHERE LabelingTasks.LabelingTaskId = %s"""
            return self.query(sql, (str(labeling_task_id),), return_dataframe=return_dataframe)

    def fetch_image_level_labels(self, task_description='UISTTurkTasks', assignment_group='TurkerTask', header=False,
                                 return_dataframe=False, **kwargs):
        """
        This method fetches
        """
        sql = """
        SELECT Assignments.AmazonTurkerId, LabelingTasks.NoLabel, LabelingTasks.Description,
        LabelingTasks.PreviousLabelingTaskId, Labels.LabelId, Labels.LabelingTaskId, LabelingTasks.TaskGSVPanoramaId,
        Labels.LabelTypeId, Labels.Deleted FROM Assignments
        INNER JOIN LabelingTasks
        ON LabelingTasks.AssignmentId = Assignments.AssignmentId
        INNER JOIN TaskPanoramas
        ON TaskPanoramas.TaskPanoramaId = LabelingTasks.TaskPanoramaId
        LEFT JOIN Labels
        ON Labels.LabelingTaskId = LabelingTasks.LabelingTaskId
        WHERE Assignments.Completed = 1
        AND Assignments.TaskDescription = %s
        AND TaskPanoramas.TaskDescription = %s
        AND (Labels.Deleted = 0 OR Labels.Deleted IS NULL)
        """

        if 'workers' in kwargs and (type(kwargs['workers']) == tuple or type(kwargs['workers']) == list) \
            and len(kwargs['workers']) > 0:
            workers = ["'%s'" % worker for worker in kwargs['workers']]
            sql += " AND (Assignments.AmazonTurkerId = " + \
                   " OR Assignments.AmazonTurkerId = ".join(workers) + ")"

        if return_dataframe:
            return self.query(sql, (assignment_group, task_description), return_dataframe=return_dataframe)
        else:
            if header:
                header_row = ("AmazonTurkerId", "NoLabel", "Description", "PreviousLabelingTaskId", "LabelId",
                              "LabelingTaskId", "GSVPanoramaId", "LabelTypeId", "Deleted")
                return [header_row] + list(self.query(sql, (assignment_group, task_description)))

            else:
                return self.query(sql, (assignment_group, task_description))

    def fetch_intersections(self, return_dataframe=False):
        """
        This method fetches the intersections
        """
        sql = "SELECT * FROM Intersections"
        return self.query(sql, return_dataframe=return_dataframe)

    def fetch_metadata(self, pano_id):
        """
        This method fetches the panorama metadata of the specified panorama
        """
        sql = "SELECT * FROM Panoramas WHERE GSVPanoramaId = %s"
        return self.query(sql, (pano_id,))

    def fetch_outlines(self, task_description='PilotTask_v2_MountPleasant', header=False,
                       neglect_deleted=False, limit=False, return_dataframe=False, **kwargs):
        """
        This method fetches the outlines provided by labelers
        !!! Use the helper function helper_clean_up_outlines(). Because the records fetched with
        this method include the outlines that are modified and outdated!!!
        """
        sql = """
        SELECT LabelingTasks.TaskGSVPanoramaId, Assignments.AmazonTurkerId, PanoYawDeg, LabelType, LabelPointId,
        Labels.LabelId, svImageX, svImageY, heading, pitch, zoom, labelLat, labelLng, Intersections.City,
        LabelingTasks.LabelingTaskId, LabelingTasks.PreviousLabelingTaskId, Labels.Deleted
        FROM LabelingTasks
        INNER JOIN Assignments
        ON Assignments.AssignmentId = LabelingTasks.AssignmentId
        INNER JOIN PanoramaProjectionProperties
        ON PanoramaProjectionProperties.GSVPanoramaId = LabelingTasks.TaskGSVPanoramaId
        INNER JOIN Labels
        ON Labels.LabelingTaskId = LabelingTasks.LabelingTaskId
        INNER JOIN LabelTypes
        ON Labels.LabelTypeId = LabelTypes.LabelTypeId
        INNER JOIN LabelPoints
        ON Labels.LabelId = LabelPoints.LabelId
        INNER JOIN TaskPanoramas
        ON LabelingTasks.TaskPanoramaId = TaskPanoramas.TaskPanoramaId
        INNER JOIN Intersections
        ON Intersections.NearestGSVPanoramaId = LabelingTasks.TaskGSVPanoramaId
        WHERE TaskPanoramas.TaskDescription = %s
        AND Assignments.Completed = 1
        """

        if 'workers' in kwargs and (type(kwargs['workers']) == tuple or type(kwargs['workers']) == list) \
            and len(kwargs['workers']) > 0:
            workers = ["'%s'" % worker for worker in kwargs['workers']]
            sql += " AND (Assignments.AmazonTurkerId = " + \
                   " OR Assignments.AmazonTurkerId = ".join(workers) + ")"

        if 'pano_ids' in kwargs and (type(kwargs['pano_ids']) == set or type(kwargs['pano_ids']) == list or
                                             type(kwargs['pano_ids']) == tuple) and len(kwargs['pano_ids']) > 0:
            pano_ids = ["'%s'" % pano_id for pano_id in kwargs['pano_ids']]
            sql += " AND (LabelingTasks.TaskGSVPanoramaId = " + \
                   " OR LabelingTasks.TaskGSVPanoramaId = ".join(pano_ids) + ")"

        if neglect_deleted:
            sql += " AND Labels.Deleted = 0"

        if 'only_original' in kwargs and kwargs['only_original']:
            sql += " AND LabelingTasks.PreviousLabelingTaskId IS NULL"

        if limit:
            sql += " LIMIT %s" % (limit)

        if return_dataframe:
            return self.query(sql, (task_description,), return_dataframe=return_dataframe)
        else:
            if header:
                header_row = ("TaskGSVPanoramaId", "AmazonTurkerId", "PanoYawDeg", "LabelType", "LabelPointId",
                              "LabelId", "svImageX", "svImageY", "heading", "pitch", "zoom", "labelLat", "labelLng",
                              "City", "LabelingTaskId", "PreviousLabelingTaskId", "Deleted")
                records = [header_row] + list(self.query(sql, (task_description,)))
                return records
            else:
                #return self.query(sql, (task_description,), lazy=True)
                return self.yield_query(sql, (task_description,))

    def fetch_quick_checks(self, task_description="UISTTurkTasks_2", assignment_group='TurkerTask',
                            return_dataframe=False):
        sql = """
        SELECT LabelingTaskInteractions.*, Assignments.AmazonTurkerId FROM LabelingTaskInteractions
        INNER JOIN LabelingTasks
        ON LabelingTasks.LabelingTaskId = LabelingTaskInteractions.LabelingTaskId
        INNER JOIN Assignments
        ON Assignments.AssignmentId = LabelingTasks.AssignmentId
        WHERE Action = %s
        AND Assignments.TaskDescription = %s
        """
        return self.query(sql, ("OnboardingQuickCheck_submitClick", assignment_group,),
                          return_dataframe=return_dataframe)

    def fetch_quick_verification_results(self, task_description="VerificationImages_v2", assignment_group="TurkerTask",
                                         return_dataframe=True):
        """
        This method returns the results of QuickVerification tasks.
        """
        sql = """SELECT Labels.LabelId, Labels.LabelGSVPanoramaId,
Assignments.AssignmentId, Assignments.AmazonTurkerId,
ValidationTaskResults.ValidationTaskResultId, ValidationTaskResults.LabelTypeId AS VerifiedLabelTypeId,
ValidationTaskResults.Timestamp
FROM TaskImages
INNER JOIN ValidationTaskResults
ON ValidationTaskResults.TaskImageId = TaskImages.TaskImageId
INNER JOIN ValidationTasks
ON ValidationTasks.ValidationTaskId = ValidationTaskResults.ValidationTaskId
INNER JOIN Assignments
ON Assignments.AssignmentId = ValidationTasks.AssignmentId
INNER JOIN Images
ON Images.ImageId = TaskImages.ImageId
INNER JOIN Labels
ON Labels.LabelId = Images.LabelId
WHERE Assignments.TaskDescription = %s AND
Assignments.Completed = 1 AND
TaskImages.TaskDescription = %s"""
        return self.query(sql, (assignment_group, task_description), return_dataframe=return_dataframe)

    def fetch_skipped_tasks(self, task_description='UISTTurkTasks', assignment_group='TurkerTask', header=False,
                            return_dataframe=False, **kwargs):
        """

        """
        sql = """
        SELECT LabelingTasks.TaskGSVPanoramaId, Assignments.AmazonTurkerId, PanoYawDeg, Intersections.City,
        LabelingTasks.LabelingTaskId, LabelingTasks.PreviousLabelingTaskId, LabelingTasks.NoLabel
        FROM LabelingTasks
        INNER JOIN Assignments
        ON Assignments.AssignmentId = LabelingTasks.AssignmentId
        INNER JOIN PanoramaProjectionProperties
        ON PanoramaProjectionProperties.GSVPanoramaId = LabelingTasks.TaskGSVPanoramaId
        INNER JOIN TaskPanoramas
        ON LabelingTasks.TaskPanoramaId = TaskPanoramas.TaskPanoramaId
        INNER JOIN Intersections
        ON Intersections.NearestGSVPanoramaId = LabelingTasks.TaskGSVPanoramaId
        WHERE LabelingTasks.NoLabel = 1
        AND Assignments.TaskDescription = %s
        AND TaskPanoramas.TaskDescription = %s
        """

        if 'workers' in kwargs and (type(kwargs['workers']) == tuple or type(kwargs['workers']) == list) and \
                        len(kwargs['workers']) > 0:
            workers = ["'%s'" % worker for worker in kwargs['workers']]
            sql += " AND (Assignments.AmazonTurkerId = " + \
                   " OR Assignments.AmazonTurkerId = ".join(workers) + ")"

        if 'pano_ids' in kwargs and (type(kwargs['pano_ids']) == set or type(kwargs['pano_ids']) == list or
                                             type(kwargs['pano_ids']) == tuple) and len(kwargs['pano_ids']) > 0:
            pano_ids = ["'%s'" % pano_id for pano_id in kwargs['pano_ids']]
            sql += " AND (LabelingTasks.TaskGSVPanoramaId = " + \
                   " OR LabelingTasks.TaskGSVPanoramaId = ".join(pano_ids) + ")"

        if return_dataframe:
            return self.query(sql, (assignment_group, task_description,), return_dataframe=return_dataframe)
        else:
            if header:
                header_row = ("TaskGSVPanoramaId", "AmazonTurkerId", "PanoYawDeg", "City", "LabelingTaskId",
                              "PreviousLabelingTaskId", "NoLabel")
                records = [header_row] + list(self.query(sql, (assignment_group, task_description,)))
                return records
            else:
                return self.yield_query(sql, (assignment_group, task_description,))

    def fetch_task_interaction(self, task_description='UISTTurkTasks', assignment_group='TurkerTask', header=False,
                               details=True, return_dataframe=True, **kwargs):
        """
        This method returns all the task interactions

        :param kwargs:
            Pass the types of interactions that you want to retrieve. If a key 'actions' is in kwargs
            (i.e., kwargs['actions'] ) and the value is tuple.
        """

        # Cache
        # http://stackoverflow.com/questions/146557/do-you-use-the-global-statement-in-python

        if details:
            # provide details of the task
            sql = """
            SELECT LabelingTaskInteractions.*, Assignments.*, LabelingTasks.PreviousLabelingTaskId,
            LabelingTasks.NoLabel
            FROM LabelingTaskInteractions
            INNER JOIN LabelingTasks
            ON LabelingTasks.LabelingTaskId = LabelingTaskInteractions.LabelingTaskId
            INNER JOIN TaskPanoramas
            ON TaskPanoramas.TaskPanoramaId = LabelingTasks.TaskPanoramaId
            INNER JOIN Assignments
            ON Assignments.AssignmentId = LabelingTasks.AssignmentId
            WHERE Assignments.Completed = 1
            AND Assignments.TaskDescription = %s
            AND TaskPanoramas.TaskDescription = %s
            """
        else:
            sql = """
            SELECT LabelingTaskInteractions.* FROM LabelingTaskInteractions
            INNER JOIN LabelingTasks
            ON LabelingTasks.LabelingTaskId = LabelingTaskInteractions.LabelingTaskId
            INNER JOIN TaskPanoramas
            ON TaskPanoramas.TaskPanoramaId = LabelingTasks.TaskPanoramaId
            INNER JOIN Assignments
            ON Assignments.AssignmentId = LabelingTasks.AssignmentId
            WHERE Assignments.Completed = 1
            AND Assignments.TaskDescription = %s
            AND TaskPanoramas.TaskDescription = %s
            """

        if 'actions' in kwargs and (type(kwargs['actions']) == tuple or type(kwargs['actions']) == list) and \
                        len(kwargs['actions']) > 0:
            actions = ["'%s'" % action for action in kwargs['actions']]
            sql += " AND (LabelingTaskInteractions.Action = " + \
                   " OR LabelingTaskInteractions.Action = ".join(actions) + ")"
        if 'pano_id' in kwargs:
            sql += " AND LabelingTaskInteractions.GSVPanoramaId = '%s'" % kwargs['pano_id']

        if return_dataframe:
            df = self.query(sql, (assignment_group, task_description), return_dataframe=return_dataframe)

            # Cache
            # http://stackoverflow.com/questions/146557/do-you-use-the-global-statement-in-python
            return df
        else:
            if header:
                if details:
                    header_row = ("LabelingTaskInteractionId", "LabelingTaskId", "Action", "GSVPanoramaId", "Lat", "Lng",
                        "Heading", "Pitch", "Zoom", "Note", "Timestamp", "AssignmentId", "AmazonTurkerId", "AmazonHitId",
                        "AmazonAssignmentId", "InterfaceType", "InterfaceVersion", "Completed", "NeedQualification",
                        "TaskDescription", "DatetimeInserted")
                else:
                    header_row = ("LabelingTaskInteractionId", "LabelingTaskId", "Action", "GSVPanoramaId", "Lat", "Lng",
                        "Heading", "Pitch", "Zoom", "Note", "Timestamp")
                return [header_row] + list(self.query(sql, (assignment_group, task_description),
                                                      return_dataframe=return_dataframe))

            else:

                df = self.query(sql, (assignment_group, task_description), return_dataframe=return_dataframe)

                return df

    def fetch_task_interactions_by_task_id(self, labeling_task_id, return_dataframe=False, **kwargs):
        if 'task_description' in kwargs:
            df = self.fetch_task_interaction(kwargs['task_description'])
            return df[df.LabelingTaskId == labeling_task_id]
        else:
            sql = """SELECT * FROM LabelingTaskInteractions WHERE LabelingTaskInteractions.LabelingTaskId = %s"""
            return self.query(sql, (str(labeling_task_id),), return_dataframe=return_dataframe)

    def fetch_labeling_task_attribute(self, labeling_task_id, attribute):
        global _sidewalk_db_cache
        key = 'SidewalkDB.fetch_labeling_task_attribute'

        if key in _sidewalk_db_cache:
            df = _sidewalk_db_cache[key]
        else:
            sql = """SELECT LabelingTaskId, Attribute, Value FROM LabelingTaskAttributes"""
            df = self.query(sql, return_dataframe=True)
            _sidewalk_db_cache[key] = df

        if len(df[(df.LabelingTaskId == labeling_task_id) & (df.Attribute == attribute)]) > 0:
            return df[(df.LabelingTaskId == 18565) & (df.Attribute == 'FalseNegative')].Value.iloc[0]
        else:
            return None


        sql = """SELECT Attribute, Value FROM LabelingTaskAttributes
        WHERE LabelingTaskId = %s AND Attribute = %s"""
        records = self.query(sql, (str(labeling_task_id), attribute))
        if len(records) > 0:
            return records[0][1]
        else:
            return None



    def fetch_task_counts(self, task_description='PilotTask_v2_MountPleasant', return_dataframe=False, **kwargs):
        """

        """
        sql = """SELECT * FROM LabelingTaskCounts
        INNER JOIN TaskPanoramas
        ON TaskPanoramas.TaskPanoramaId = LabelingTaskCounts.TaskPanoramaId
        WHERE TaskDescription = %s"""
        return self.query(sql, (task_description,), return_dataframe=return_dataframe)

    def fetch_task_panorama(self, panorama_id, task_description, return_dataframe=False, **kwargs):
        """
        This method returns the specified task panorama
        """
        sql = """SELECT * FROM TaskPanoramas
        WHERE TaskPanoramas.TaskGSVPanoramaId = %s
        AND TaskPanoramas.TaskDescription = %s"""
        return self.query(sql, (panorama_id, task_description), return_dataframe=return_dataframe)

    def fetch_task_panoramas(self, task_description='PilotTask_v2_MountPleasant', header=False, **kwargs):
        """
        This method returns the all the task panoramas

        :param task_description:
            The task group name. I've been using PilotTask_v2_MountPleasant although the area is not limited to
            Mt. Pleasant and is misleading. Because I am lazy to update all the code accordingly
        :param header:
            If header is true, it inserts the header too.
        :return:
            Task panoramas
        """

        if "with_id" in kwargs and kwargs["with_id"]:
            if "VerificationImages" in task_description:
                sql = """SELECT TaskPanoramas.TaskPanoramaId, TaskPanoramas.TaskGSVPanoramaId, Intersections.City,
                TaskPanoramas.TaskDescription FROM TaskImages
                INNER JOIN Images
                ON Images.ImageId = TaskImages.ImageId
                INNER JOIN Labels
                ON Labels.LabelId = Images.LabelId
                INNER JOIN LabelingTasks
                ON LabelingTasks.LabelingTaskId = Labels.LabelingTaskId
                INNER JOIN TaskPanoramas
                ON TaskPanoramas.TaskPanoramaId = LabelingTasks.TaskPanoramaId
                INNER JOIN Intersections
                ON Intersections.NearestGSVPanoramaId = TaskPanoramas.TaskGSVPanoramaId
                WHERE TaskImages.TaskDescription = %s
                """
            else:
                sql = """
                SELECT TaskPanoramas.TaskPanoramaId, TaskPanoramas.TaskGSVPanoramaId, Intersections.City,
                TaskPanoramas.TaskDescription FROM TaskPanoramas
                INNER JOIN Intersections
                ON Intersections.NearestGSVPanoramaId = TaskPanoramas.TaskGSVPanoramaId
                WHERE TaskDescription = %s
                """
            if header:
                header_row = ('TaskPanoramaId', 'GSVPanoramaId', 'City', 'TaskDescription')
                records = [header_row] + list(self.query(sql, (task_description,)))
                return records
            else:
                return self.query(sql, (task_description,))
        else:
            if "VerificationImages" in task_description:
                sql = """SELECT TaskPanoramas.TaskGSVPanoramaId, Intersections.City, TaskPanoramas.TaskDescription FROM TaskImages
                INNER JOIN Images
                ON Images.ImageId = TaskImages.ImageId
                INNER JOIN Labels
                ON Labels.LabelId = Images.LabelId
                INNER JOIN LabelingTasks
                ON LabelingTasks.LabelingTaskId = Labels.LabelingTaskId
                INNER JOIN TaskPanoramas
                ON TaskPanoramas.TaskPanoramaId = LabelingTasks.TaskPanoramaId
                INNER JOIN Intersections
                ON Intersections.NearestGSVPanoramaId = TaskPanoramas.TaskGSVPanoramaId
                WHERE TaskImages.TaskDescription = %s
                """
            else:
                sql = """
                SELECT TaskPanoramas.TaskGSVPanoramaId, Intersections.City, TaskPanoramas.TaskDescription
                FROM TaskPanoramas
                INNER JOIN Intersections
                ON Intersections.NearestGSVPanoramaId = TaskPanoramas.TaskGSVPanoramaId
                WHERE TaskDescription = %s
                """
            if header:
                header_row = ('GSVPanoramaId', 'City', 'TaskDescription')
                records = [header_row] + list(self.query(sql, (task_description,)))
                return records
            else:
                return self.query(sql, (task_description,))

    def fetch_tasks(self, task_description='PilotTask_v2_MountPleasant', return_dataframe=False, **kwargs):
        """

        """
        sql = """SELECT * FROM TaskPanoramas WHERE TaskDescription = %s"""
        return self.query(sql, (task_description,), return_dataframe=return_dataframe)

    def get_last_row_id(self):
        """ 
        This method returns the lastrowid
        """
        return self._cur.lastrowid

    def insert_assignment(self, amazon_worker_id, amazon_hit_id='DefaultValue', amazon_assignment_id='DefaultValue',
                          interface_type='', interface_version='', completed='0', need_qualification='0',
                          assignment_description=''):
        sql = "SELECT * FROM Assignments WHERE AmazonTurkerId = %s"
        records = self.query(sql, (amazon_worker_id))
        if len(records) > 0:
            assignment_id = records[0][0]
            return assignment_id
        else:
            sql = """INSERT Assignments (AmazonTurkerId, AmazonHitId, AmazonAssignmentId, InterfaceType,
            InterfaceVersion, Completed, NeedQualification, TaskDescription)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """
            self.query(sql, (amazon_worker_id, amazon_hit_id, amazon_assignment_id, interface_type, interface_version,
                             completed, need_qualification, assignment_description))
            return None

    def insert_detected_bounding_box(self, record, bin_description):
        """
        This method takes a record 
        
        :param record:
            Record should have {'assignments': [...], 'labeling_tasks': [...], 'labels': [...], 'label_points': [...]}
        :param bin_description:
            Description of the detected bounding boxes. 
        """
        sql = """SELECT * FROM Assignments WHERE AmazonTurkerId=%s"""
        for asg in record['assignments']:
            amazon_turker_id = str(asg[0])
            cur_data = self.query(sql, (amazon_turker_id,))
            
            if len(cur_data) == 0:
                sql = """
                INSERT INTO Assignments
                (AmazonTurkerId, AmazonHitId, AmazonAssignmentId,
                InterfaceType, InterfaceVersion, Completed, TaskDescription)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """
                self._cur.execute(sql, (asg))
                asg_id = self._cur.lastrowid
            else:
                asg_id = cur_data[0][0]
        
        sql = """SELECT * FROM LabelingTasks WHERE AssignmentId=%s AND TaskGSVPanoramaId=%s"""
        for task in record['labeling_tasks']:
            task_gsv_panorama_id = task[0]
            records = self.query(sql, (asg_id, task_gsv_panorama_id))
            if len(records) == 0:
                sql = """
                INSERT INTO LabelingTasks (AssignmentId, TaskGSVPanoramaId, NoLabel, Description)
                VALUES (%s, %s, %s, %s)
                """
                self._cur.execute(sql, (asg_id, task[0], task[1], task[2]))
                task_id = self._cur.lastrowid
            else:
                task_id = records[0][0]

        #
        # Go through all the labels and points and check if the exact same points exist or not.
        # If so, do not enter results.
        for label in record['labels']:
            sql = """
            INSERT INTO Labels (LabelingTaskid, LabelGSVPanoramaId, LabelTypeId, Deleted)
            VALUES (%s, %s, %s, %s)
            """
            cur_data = self._cur.execute(sql, (task_id, label[0], label[1], label[2]))
            label_id = self._cur.lastrowid
        
        sql = """
        INSERT INTO LabelPoints (LabelId, svImageX, svImageY, heading, pitch, zoom, labelLat, labelLng)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """
        for label_point in record['label_points']:
            cur_data = self._cur.execute(sql, (label_id, label_point[0], label_point[1], label_point[2], label_point[3],
                                               label_point[4], label_point[5], label_point[6]))
        
        sql = """INSERT INTO BinnedLabels (LabelBinId, LabelId) VALUES (%s, %s)"""
        return

    def insert_intersections(self, data, force=False):
        """
        This function takes data returned by get_nearest_pano_metadata() helper function
        If force is True, insert the bus stop data even if the bus sotp already exists. 
        """
        # Check if the bus stop record already exist
        if not force:
            sql = """SELECT * FROM Intersections WHERE NearestGSVPanoramaId=%s"""
            cur_data = self._cur.execute(sql, (data['data_properties']['pano_id']))
            if cur_data != 0:
                raise RecordError('The record already exists.')
        
        num_links = str(len(data['links']))
        sql = """INSERT INTO Intersections (Lat, Lng, NearestGSVPanoramaId, NumberOfLinks) VALUES (%s, %s, %s, %s)"""
        self._cur.execute(sql, (data['intersection']['lat'], data['intersection']['lng'],
                                data['data_properties']['pano_id'], num_links))
        return

    def insert_label(self, labeling_task_id, gsv_panorama_id, label_type_id, deleted):
        """
        This method inserts a label into the Labels table
        """
        sql = """INSERT Labels (LabelingTaskId, LabelGSVPanoramaId, LabelTypeId, Deleted)
                    VALUES (%s, %s, %s, %s)"""
        self.query(sql, (labeling_task_id, gsv_panorama_id, label_type_id, deleted))
        return

    def insert_label_point(self, label_id, x, y, heading, pitch, zoom, lat, lng):
        """
        This method inserts a label point into the LabelPoints table
        """
        sql = """INSERT INTO LabelPoints (LabelId, svImageX, svImageY, heading, pitch, zoom, labelLat, labelLng)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"""
        self.query(sql, (label_id, x, y, heading, pitch, zoom, lat, lng))
        return

    def insert_label_confidence(self, label_id, confidence):
        """
        This method inserts a label confidence record into the LabelConfidenceScores table
        """
        sql = "INSERT INTO LabelConfidenceScores (LabelId, ConfidenceScore) VALUES (%s, %s)"
        self.query(sql, (label_id, confidence))
        return

    def insert_labeling_task(self, assignment_id, task_panorama_id, gsv_panorama_id, no_label, description):
        """
        This method inserts a LabelingTask record
        """
        sql = "SELECT LabelingTaskId FROM LabelingTasks WHERE TaskPanoramaId = %s"
        records = self.query(sql, (str(task_panorama_id)))
        if len(records) > 0:
            labeling_task_id = records[0][0]
            return labeling_task_id
        else:
            sql = """INSERT LabelingTasks (AssignmentId, TaskPanoramaId, TaskGSVPanoramaId, NoLabel, Description)
            VALUES (%s, %s, %s, %s, %s)"""

            self.query(sql, (str(assignment_id), str(task_panorama_id), gsv_panorama_id, str(no_label), description))
            labeling_task_id = self.get_last_row_id()
            return None

    def insert_nearby_panorama(self, data, force=False):
        """
        This function takes one item from the data returned by get_nearby_pano_ids() helper function
        If force is True, insert the bus stop data even if the bus sotp already exists. 
        """
        # Check if the bus stop record already exist
        if not force:
            sql = """SELECT * FROM NearbyPanoramas WHERE TaskGSVPanoramaId=%s AND GSVPanoramaId=%s"""
            cur_data = self._cur.execute(sql, (data['origin_pano_id'], data['pano_id']))
            if cur_data != 0:
                raise RecordError('The record already exists.')
        
        sql = """INSERT INTO NearbyPanoramas (TaskGSVPanoramaId, GSVPanoramaId, StepSize) VALUES (%s, %s, %s)"""
        self._cur.execute(sql, (data['origin_pano_id'], data['pano_id'], data['step_size']))
        return
    
    def insert_panoramas(self, data):
        """
        This function takes data returned by get_nearest_pano_metadata() helper function
        
        :param data: An object that holds panoarama data
        """
        # Check if the record already exists
        sql = """SELECT * FROM Panoramas WHERE GSVPanoramaId=%s"""
        cur_data = self._cur.execute(sql, (data['data_properties']['pano_id']))
        if cur_data != 0:
            raise RecordError('The record already exists.')
        
        sql = """INSERT INTO Panoramas (
                GSVPanoramaId, 
                ImageWidth, 
                ImageHeight, 
                TileWidth, 
                TileHeight, 
                ImageDate, 
                NumZoomLevels, 
                Lat, 
                Lng, 
                OriginalLat, 
                OriginalLng, 
                ElevationWgs84M, 
                Copyright, 
                Text, 
                StreetRange, 
                Region, 
                Country) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"""
        
        # 
        if 'elevation_wgs84_m' not in data['data_properties']:
            data['data_properties']['elevation_wgs84_m'] = 'undefined'
        if 'image_date' not in data['data_properties']:
            data['data_properties']['image_date'] = 'undefined'
            print('image_date undefined')
        
        self._cur.execute(sql, (str(data['data_properties']['pano_id']),
                                str(data['data_properties']['image_width']),
                                str(data['data_properties']['image_height']),
                                str(data['data_properties']['tile_width']),
                                str(data['data_properties']['tile_height']),
                                str(data['data_properties']['image_date']),
                                str(data['data_properties']['num_zoom_levels']),
                                str(data['data_properties']['lat']),
                                str(data['data_properties']['lng']),
                                str(data['data_properties']['original_lat']),
                                str(data['data_properties']['original_lng']),
                                str(data['data_properties']['elevation_wgs84_m']),
                                str(data['data_properties']['copyright']),
                                str(data['data_properties']['text']),
                                str(data['data_properties']['street_range']),
                                str(data['data_properties']['region']),
                                str(data['data_properties']['country'])))
        return

    def insert_panorama_projection_properties(self, data):
        """
        This function takes data returned by get_nearest_pano_metadata() helper function
        """
        # Check if the record already exists
        sql = """SELECT * FROM PanoramaProjectionProperties WHERE GSVPanoramaId=%s"""
        cur_data = self._cur.execute(sql, (data['data_properties']['pano_id']))
        if cur_data != 0:
            raise RecordError('The record already exists.')
        
        sql = """INSERT INTO PanoramaProjectionProperties (
                GSVPanoramaId, 
                ProjectionType,
                PanoYawDeg,
                TiltYawDeg,
                TiltPitchDeg) VALUES (%s,%s,%s,%s,%s)"""
        
        self._cur.execute(sql, (str(data['data_properties']['pano_id']),
                                str(data['projection_properties']['projection_type']),
                                str(data['projection_properties']['pano_yaw_deg']),
                                str(data['projection_properties']['tilt_yaw_deg']),
                                str(data['projection_properties']['tilt_pitch_deg'])))
        return

    def insert_panorama_links(self, data):
        """
        This function takes data returned by get_nearest_pano_metadata() helper function
        """
        sql = """SELECT * FROM PanoramaLinks WHERE SourceGSVPanoramaId=%s"""
        cur_data = self._cur.execute(sql, (data['data_properties']['pano_id']))
        if cur_data != 0:
            raise RecordError('The record already exists.')
        
        sql = """INSERT INTO PanoramaLinks (
                SourceGSVPanoramaId, 
                TargetGSVPanoramaId,
                YawDeg,
                RoadArgb,
                Scene,
                LinkText) VALUES (%s,%s,%s,%s,%s,%s)"""

        for link in data['links']:
            self._cur.execute(sql, (str(data['data_properties']['pano_id']),
                                    link['pano_id'],
                                    link['yaw_deg'],
                                    link['road_argb'],
                                    link['scene'],
                                    link['link_text']))
        return
    
    def insert_task_panorama(self, panorama_id, task_description):
        """
         This function inserts each of data returned by get_task_panoramas into TaskPanorama
        """
        sql = """SELECT * FROM TaskPanoramas WHERE TaskGSVPanoramaId=%s AND TaskDescription=%s"""
        records = self.query(sql, (panorama_id, task_description))
        if len(records) > 0:
            task_panorama_id = records[0][0]
            return task_panorama_id
        else:
            sql = "INSERT TaskPanoramas (TaskGSVPanoramaId, TaskDescription) VALUES (%s, %s)"
            self.query(sql, (panorama_id, task_description))
            return None

    def insert_labeling_task_counts(self, task_panorama_id):
        sql = "INSERT LabelingTaskCounts (TaskPanoramaId) VALUES (%s)"
        self.query(sql, (task_panorama_id,))
        return None

    def query(self, sql, data=None, return_dataframe=False):
        """
        This function runs an arbitrary query.

        Returning dataframes
        http://pandas.pydata.org/pandas-docs/stable/io.html#sql-queries
        """
        try:
            if data:
                if return_dataframe:
                    data = tuple(["'%s'" % str(item) for item in data])
                    sql = sql % data
                    return pdsql.frame_query(sql, self._conn)
                else:
                    self._cur.execute(sql, data)
                    return self._cur.fetchall()
            else:
                if return_dataframe:
                    return pdsql.frame_query(sql, self._conn)
                else:
                    self._cur.execute(sql)
                    return self._cur.fetchall()
        except:
            raise

    def yield_query(self, sql, data=None):
        """
        This function runs an arbitrary query.
        """
        if data:
            self._cur.execute(sql, data)
        else:
            self._cur.execute(sql)

        #
        # Lazy fetching
        # https://github.com/PyMySQL/PyMySQL/blob/master/pymysql/cursors.py
        # return self._cur.fetchall_unbuffered()
        #
        # Or actually, return a generator using fetchmany()
        N = 3000
        while True:
            records = self._cur.fetchmany(N)
            yield records
            if len(records) < N:
                break

if __name__ == '__main__':
    print("SidewalkDB.py")

    from SidewalkUtilities import modify_city_name
    with SidewalkDB() as db:
        df = db.fetch_labeling_tasks('VerificationExperiment_VerifyHumanLabels_2', assignment_description='TurkerTask',
                                    return_dataframe=True)
        previous_task_ids = df.PreviousLabelingTaskId[~df.PreviousLabelingTaskId.isnull()].unique()
        df = db.fetch_labeling_tasks_with_task_ids(map(int, list(previous_task_ids)))
        print(df)
