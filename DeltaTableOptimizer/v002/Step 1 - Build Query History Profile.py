# Databricks notebook source
import json
import sqlparse
from sql_metadata import Parser
import requests
import re
import os
from datetime import datetime, timedelta, timezone
from pyspark.sql import functions as F
from pyspark.sql.types import *
from pyspark.sql import SparkSession

class QueryProfiler():
    
    def __init__(self, workspace_url, warehouse_ids, database_name="delta_optimizer"):
        
        ## Assumes running on a spark environment
        
        self.workspace_url = workspace_url.strip()
        self.warehouse_ids_list = [i.strip() for i in warehouse_ids]
        self.warehouse_ids = ",".join(self.warehouse_ids_list)
        self.database_name = database_name
        self.spark = SparkSession.getActiveSession()
        
        print(f"Initializing Delta Optimizer for: {self.workspace_url}\n Monitoring SQL Warehouses: {self.warehouse_ids} \n Database Location: {self.database_name}")
        ### Initialize Tables on instantiation
        
        self.spark.sql(f"""CREATE DATABASE IF NOT EXISTS {self.database_name};""")
        # Query History Log
        self.spark.sql(f"""CREATE TABLE IF NOT EXISTS {self.database_name}.query_history_log 
                           (Id BIGINT GENERATED ALWAYS AS IDENTITY,
                           WarehouseIds ARRAY<STRING>,
                           WorkspaceName STRING,
                           StartTimestamp TIMESTAMP,
                           EndTimestamp TIMESTAMP)
                           USING DELTA""")
        
        self.spark.sql(f"""CREATE TABLE IF NOT EXISTS {self.database_name}.raw_query_history_statistics
                        (Id BIGINT GENERATED ALWAYS AS IDENTITY,
                        query_id STRING,
                        query_start_time_ms FLOAT,
                        query_end_time_ms FLOAT,
                        duration FLOAT,
                        query_text STRING,
                        status STRING,
                        statement_type STRING,
                        rows_produced FLOAT,
                        metrics MAP<STRING, FLOAT>)
                        USING DELTA""")
        
        self.spark.sql(f"""CREATE TABLE IF NOT EXISTS {self.database_name}.parsed_distinct_queries
                        (
                        Id BIGINT GENERATED ALWAYS AS IDENTITY,
                        query_id STRING,
                        query_text STRING,
                        profiled_columns ARRAY<STRING>
                        )
                        USING DELTA""")
        
        return
    
    
    ## Spark SQL Tree Parsing Udfs    
    ## Input Filter Type can be : where, join, group_by
    @staticmethod
    @udf("array<string>")
    def getParsedFilteredColumnsinSQL(sqlString):

        ## output ["table_name:column_name,table_name:colunmn:name"]
        final_table_map = []

        try: 
            results = Parser(sqlString)

            final_columns = []

            ## If just a select, then skip this and just return the table
            try:
                final_columns.append(results.columns_dict.get("where"))
                final_columns.append(results.columns_dict.get("join"))
                final_columns.append(results.columns_dict.get("group_by"))

            except:
                for tbl in results.tables:
                    final_table_map.append(f"{tbl}:")

            final_columns_clean = [i for i in final_columns if i is not None]
            flatted_final_cols = list(set([x for xs in final_columns_clean for x in xs]))

            ## Try to map columns to specific tables for simplicity downstream

            """Rules -- this isnt a strict process cause we will filter things later, 
            what needs to happen is we need to get all possible columns on a given table, even if not true

            ## Aliases are already parsed so the column is either fully qualified or fully ambiguous
            ## Assign a column to table if: 
            ## 1. column has an explicit alias to it
            ## 2. column is not aliased
            """

            for tbl in results.tables:
                found_cols = []
                for st in flatted_final_cols:

                    ## Get Column Part
                    try:
                        column_val = st[st.rindex('.')+1:] 
                    except: 
                        column_val = st

                    ## Get Table Part
                    try:
                        table_val = st[:st.rindex('.')] or None
                    except:
                        table_val = None

                    ## Logic that add column if tbl name is found or if there was no specific table name for the column
                    if st.find(tbl) >= 0 or (table_val is None):
                        if column_val is not None and len(column_val) > 1:
                            final_table_map.append(f"{tbl}:{column_val}")

        except Exception as e:
            final_table_map = [str(f"ERROR: {str(e)}")]

        return final_table_map

    @staticmethod
    @udf("integer")
    def checkIfJoinColumn(sqlString, columnName):
        try: 
            results = Parser(sqlString)

            ## If just a select, then skip this and just return the table
            if columnName in results.columns_dict.get("join"):
                return 1
            else:
                return 0
        except:
            return 0


    @staticmethod
    @udf("integer")
    def checkIfFilterColumn(sqlString, columnName):
        try: 
            results = Parser(sqlString)

            ## If just a select, then skip this and just return the table
            if columnName in results.columns_dict.get("where"):
                return 1
            else:
                return 0
        except:
            return 0

    @staticmethod
    @udf("integer")
    def checkIfGroupColumn(sqlString, columnName):
        try: 
            results = Parser(sqlString)

            ## If just a select, then skip this and just return the table
            if columnName in results.columns_dict.get("group_by"):
                return 1
            else:
                return 0
        except:
            return 0

    ## Convert timestamp to milliseconds for API
    @staticmethod
    def ms_timestamp(dt):
        return int(round(dt.replace(tzinfo=timezone.utc).timestamp() * 1000, 0))
    
    
    ## Get Start and End range for Query History API
    def get_time_series_lookback(self, lookback_period):
        
        ## Gets time series from current timestamp to now - lookback period - if overrride
        end_timestamp = datetime.now()
        start_timestamp = end_timestamp - timedelta(days = lookback_period)
        ## Convert to ms
        start_ts_ms = self.ms_timestamp(start_timestamp)
        end_ts_ms = self.ms_timestamp(end_timestamp)
        print(f"Getting Query History to parse from period: {start_timestamp} to {end_timestamp}")
        return start_ts_ms, end_ts_ms
 

    ## If no direct time ranges provided (like after a first load, just continue where the log left off)
    def get_most_recent_history_from_log(self, mode='auto', lookback_period=3):
      
        ## This function gets the most recent end timestamp of the query history range, and returns new range from then to current timestamp
        
        start_timestamp = self.spark.sql(f"""SELECT MAX(EndTimestamp) FROM {self.database_name}.query_history_log""").collect()[0][0]
        end_timestamp = datetime.now()
        
        if (start_timestamp is None or mode != 'auto'): 
            if mode == 'auto' and start_timestamp is None:
                print(f"""Mode is auto and there are no previous runs in the log... using lookback period from today: {lookback_period}""")
            elif mode != 'auto' and start_timestamp is None:
                print(f"Manual time interval with lookback period: {lookback_period}")
                
            return self.get_time_series_lookback(lookback_period)
        
        else:
            start_ts_ms = self.ms_timestamp(start_timestamp)
            end_ts_ms = self.ms_timestamp(end_timestamp)
            print(f"Getting Query History to parse from most recent pull at: {start_timestamp} to {end_timestamp}")
            return start_ts_ms, end_ts_ms
    
    
    ## Insert a query history pull into delta log to track state
    def insert_query_history_delta_log(self, start_ts_ms, end_ts_ms):

        ## Upon success of a query history pull, this function logs the start_ts and end_ts that it was pulled into the logs

        try: 
            spark.sql(f"""INSERT INTO {self.database_name}.query_history_log (WarehouseIds, WorkspaceName, StartTimestamp, EndTimestamp)
                               VALUES(split('{self.warehouse_ids}', ','), 
                               '{self.workspace_url}', ('{start_ts_ms}'::double/1000)::timestamp, 
                               ('{end_ts_ms}'::double/1000)::timestamp)
            """)
        except Exception as e:
            raise(e)
    
    
    ## Clear database and start over
    def truncate_delta_optimizer_results(self):
        
        print(f"Truncating database (and all tables within): {self.database_name}...")
        df = self.spark.sql(f"""SHOW TABLES IN {self.database_name}""").filter(F.col("database") == F.lit(self.database_name)).select("tableName").collect()

        for i in df: 
            table_name = i[0]
            print(f"Deleting Table: {table_name}...")
            self.spark.sql(f"""TRUNCATE TABLE {self.database_name}.{table_name}""")
            
        print(f"Database: {self.database_name} successfully Truncated!")
        return
    

    ## Run the Query History Pull (main function)
    def build_query_history_profile(self, dbx_token, mode='auto', lookback_period_days=3):
        
        ## Modes are 'auto' and 'manual' - auto, means it manages its own state, manual means you override the time frame to analyze no matter the history
        lookback_period = int(lookback_period_days)
        warehouse_ids_list = self.warehouse_ids_list
        workspace_url = self.workspace_url

        print(f"""Loading Query Profile to delta from workspace: {workspace_url} \n 
              from Warehouse Ids: {warehouse_ids_list} \n for the last {lookback_period} days...""")
        
        ## Get time series range based on 
        ## If override = True, then choose lookback period in days
        start_ts_ms, end_ts_ms = self.get_most_recent_history_from_log(mode, lookback_period)
        
        ## Put together request 
        
        request_string = {
            "filter_by": {
              "query_start_time_range": {
              "end_time_ms": end_ts_ms,
              "start_time_ms": start_ts_ms
            },
            "statuses": [
                "FINISHED", "CANCELED"
            ],
            "warehouse_ids": warehouse_ids_list
            },
            "include_metrics": "true",
            "max_results": "1000"
        }

        ## Convert dict to json
        v = json.dumps(request_string)
        
        uri = f"https://{workspace_url}/api/2.0/sql/history/queries"
        headers_auth = {"Authorization":f"Bearer {dbx_token}"}

        
        ## This file could be large
        ## Convert response to dict
        
        #### Get Query History Results from API
        endp_resp = requests.get(uri, data=v, headers=headers_auth).json()
        
        initial_resp = endp_resp.get("res")
        
        if initial_resp is None:
            print(f"DBSQL Has no queries on the warehouse for these times:{start_ts_ms} - {end_ts_ms}")
            initial_resp = []
            ## Continue anyways cause there could be old queries and we want to still compute aggregates
        
        
        next_page = endp_resp.get("next_page_token")
        has_next_page = endp_resp.get("has_next_page")
        

        if has_next_page is True:
            print(f"Has next page?: {has_next_page}")
            print(f"Getting next page: {next_page}")

        ## Page through results   
        page_responses = []

        while has_next_page is True: 

            print(f"Getting results for next page... {next_page}")

            raw_page_request = {
            "include_metrics": "true",
            "max_results": 1000,
            "page_token": next_page
            }

            json_page_request = json.dumps(raw_page_request)

            ## This file could be large
            current_page_resp = requests.get(uri,data=json_page_request, headers=headers_auth).json()
            current_page_queries = current_page_resp.get("res")

            ## Add Current results to total results or write somewhere (to s3?)

            page_responses.append(current_page_queries)

            ## Get next page
            next_page = current_page_resp.get("next_page_token")
            has_next_page = current_page_resp.get("has_next_page")

            if has_next_page is False:
                break

                
        ## Coaesce all responses     
        all_responses = [x for xs in page_responses for x in xs] + initial_resp
        print(f"Saving {len(all_responses)} Queries To Delta for Profiling")

        
        ## Get responses and save to Delta 
        raw_queries_df = (spark.createDataFrame(all_responses))
        raw_queries_df.createOrReplaceTempView("raw")
        
        ## Start Profiling Process
        try: 
            self.spark.sql(f"""INSERT INTO {self.database_name}.raw_query_history_statistics (query_id,query_start_time_ms, query_end_time_ms, duration, query_text, status, statement_type,rows_produced,metrics)
                        SELECT
                        query_id,
                        query_start_time_ms,
                        query_end_time_ms,
                        duration,
                        query_text,
                        status,
                        statement_type,
                        rows_produced,
                        metrics
                        FROM raw
                        WHERE statement_type = 'SELECT';
                        """)
            
            ## If successfull, insert log
            self.insert_query_history_delta_log(start_ts_ms, end_ts_ms)
            
            
            ## Build Aggregate Summary Statistics with old and new queries
            self.spark.sql("""
                --Calculate Query Statistics to get importance Rank by Query (duration, rows_returned)
                -- This is an AGGREGATE table that needs to be rebuilt every time from the source -- not incremental
                CREATE OR REPLACE TABLE delta_optimizer.query_summary_statistics
                AS (
                  WITH raw_query_stats AS (
                    SELECT query_id,
                    AVG(duration) AS AverageQueryDuration,
                    AVG(rows_produced) AS AverageRowsProduced,
                    COUNT(*) AS TotalQueryRuns,
                    AVG(duration)*COUNT(*) AS DurationTimesRuns
                    FROM delta_optimizer.raw_query_history_statistics
                    WHERE status IN('FINISHED', 'CANCELED')
                    AND statement_type = 'SELECT'
                    GROUP BY query_id
                  )
                  SELECT 
                  *
                  FROM raw_query_stats
                )
                """)
            
            ## Parse SQL Query and Save into parsed distinct queries table
            df = self.spark.sql(f"""SELECT DISTINCT query_id, query_text FROM {self.database_name}.raw_query_history_statistics""")

            df_profiled = (df.withColumn("profiled_columns", self.getParsedFilteredColumnsinSQL(F.col("query_text")))
                    )

            df_profiled.createOrReplaceTempView("new_parsed")
            self.spark.sql(f"""
                MERGE INTO {self.database_name}.parsed_distinct_queries AS target
                USING new_parsed AS source
                ON source.query_id = target.query_id
                WHEN MATCHED THEN UPDATE SET target.query_text = source.query_text
                WHEN NOT MATCHED THEN 
                    INSERT (target.query_id, target.query_text, target.profiled_columns) 
                    VALUES (source.query_id, source.query_text, source.profiled_columns)""")
            
            ## Calculate statistics on profiled queries

            pre_stats_df = (self.spark.sql(f"""
                  WITH exploded_parsed_cols AS (SELECT DISTINCT
                  explode(profiled_columns) AS explodedCols,
                  query_id,
                  query_text
                  FROM {self.database_name}.parsed_distinct_queries
                  ),

                  step_2 AS (SELECT DISTINCT
                  split(explodedCols, ":")[0] AS TableName,
                  split(explodedCols, ":")[1] AS ColumnName,
                  root.query_text,
                  hist.*
                  FROM exploded_parsed_cols AS root
                  LEFT JOIN {self.database_name}.query_summary_statistics AS hist USING (query_id)--SELECT statements only included
                  )

                  SELECT *,
                  size(split(query_text, ColumnName)) - 1 AS NumberOfColumnOccurrences
                  FROM step_2
                """)
                .withColumn("isUsedInJoin", self.checkIfJoinColumn(F.col("query_text"), F.concat(F.col("TableName"), F.lit("."), F.col("ColumnName"))))
                .withColumn("isUsedInFilter", self.checkIfFilterColumn(F.col("query_text"), F.concat(F.col("TableName"), F.lit("."), F.col("ColumnName"))))
                .withColumn("isUsedInGroup", self.checkIfGroupColumn(F.col("query_text"), F.concat(F.col("TableName"), F.lit("."), F.col("ColumnName"))))
                )

            pre_stats_df.createOrReplaceTempView("withUseFlags")

            self.spark.sql(f"""
            CREATE OR REPLACE TABLE {self.database_name}.query_column_statistics
            AS SELECT * FROM withUseFlags
            """)

            #### Calculate more statistics

            self.spark.sql(f"""CREATE OR REPLACE TABLE {self.database_name}.read_statistics_column_level_summary
                    AS
                    WITH test_q AS (
                        SELECT * FROM {self.database_name}.query_column_statistics
                        WHERE length(ColumnName) >= 1 -- filter out queries with no joins or predicates TO DO: Add database filtering here

                    ),
                    step_2 AS (
                        SELECT 
                        TableName,
                        ColumnName,
                        MAX(isUsedInJoin) AS isUsedInJoin,
                        MAX(isUsedInFilter) AS isUsedInFilter,
                        MAX(isUsedInGroup) AS isUsedInGroup,
                        SUM(isUsedInJoin) AS NumberOfQueriesUsedInJoin,
                        SUM(isUsedInFilter) AS NumberOfQueriesUsedInFilter,
                        SUM(isUsedInGroup) AS NumberOfQueriesUsedInGroup,
                        COUNT(DISTINCT query_id) AS QueryReferenceCount,
                        SUM(DurationTimesRuns) AS RawTotalRuntime,
                        AVG(AverageQueryDuration) AS AvgQueryDuration,
                        SUM(NumberOfColumnOccurrences) AS TotalColumnOccurrencesForAllQueries,
                        AVG(NumberOfColumnOccurrences) AS AvgColumnOccurrencesInQueryies
                        FROM test_q
                        WHERE length(ColumnName) >=1
                        GROUP BY TableName, ColumnName
                    )
                    SELECT * FROM step_2
                    ; """)


            #### Standard scale the metrics 
            df = self.spark.sql(f"""SELECT * FROM {self.database_name}.read_statistics_column_level_summary""")

            columns_to_scale = ["QueryReferenceCount", 
                                "RawTotalRuntime", 
                                "AvgQueryDuration", 
                                "TotalColumnOccurrencesForAllQueries", 
                                "AvgColumnOccurrencesInQueryies"]

            min_exprs = {x: "min" for x in columns_to_scale}
            max_exprs = {x: "max" for x in columns_to_scale}

            ## Apply basic min max scaling by table for now

            dfmin = df.groupBy("TableName").agg(min_exprs)
            dfmax = df.groupBy("TableName").agg(max_exprs)

            df_boundaries = dfmin.join(dfmax, on="TableName", how="inner")

            df_pre_scaled = df.join(df_boundaries, on="TableName", how="inner")

            df_scaled = (df_pre_scaled
                     .withColumn("QueryReferenceCountScaled", F.coalesce((F.col("QueryReferenceCount") - F.col("min(QueryReferenceCount)"))/(F.col("max(QueryReferenceCount)") - F.col("min(QueryReferenceCount)")), F.lit(0)))
                     .withColumn("RawTotalRuntimeScaled", F.coalesce((F.col("RawTotalRuntime") - F.col("min(RawTotalRuntime)"))/(F.col("max(RawTotalRuntime)") - F.col("min(RawTotalRuntime)")), F.lit(0)))
                     .withColumn("AvgQueryDurationScaled", F.coalesce((F.col("AvgQueryDuration") - F.col("min(AvgQueryDuration)"))/(F.col("max(AvgQueryDuration)") - F.col("min(AvgQueryDuration)")), F.lit(0)))
                     .withColumn("TotalColumnOccurrencesForAllQueriesScaled", F.coalesce((F.col("TotalColumnOccurrencesForAllQueries") - F.col("min(TotalColumnOccurrencesForAllQueries)"))/(F.col("max(TotalColumnOccurrencesForAllQueries)") - F.col("min(TotalColumnOccurrencesForAllQueries)")), F.lit(0)))
                     .withColumn("AvgColumnOccurrencesInQueriesScaled", F.coalesce((F.col("AvgColumnOccurrencesInQueryies") - F.col("min(AvgColumnOccurrencesInQueryies)"))/(F.col("max(AvgColumnOccurrencesInQueryies)") - F.col("min(AvgColumnOccurrencesInQueryies)")), F.lit(0)))
                     .selectExpr("TableName", "ColumnName", "isUsedInJoin", "isUsedInFilter","isUsedInGroup","NumberOfQueriesUsedInJoin","NumberOfQueriesUsedInFilter","NumberOfQueriesUsedInGroup","QueryReferenceCount", "RawTotalRuntime", "AvgQueryDuration", "TotalColumnOccurrencesForAllQueries", "AvgColumnOccurrencesInQueryies", "QueryReferenceCountScaled", "RawTotalRuntimeScaled", "AvgQueryDurationScaled", "TotalColumnOccurrencesForAllQueriesScaled", "AvgColumnOccurrencesInQueriesScaled")
                        )


            df_scaled.createOrReplaceTempView("final_scaled_reads")

            self.spark.sql(f"""CREATE OR REPLACE TABLE {self.database_name}.read_statistics_scaled_results 
            AS
            SELECT * FROM final_scaled_reads""")


            print(f"""Completed Query Profiling! Results can be found here:\n
            SELECT * FROM {self.database_name}.read_statistics_scaled_results""")

            return
            
        except Exception as e:
            raise(e)
            
            
            
            
## Insert a query history pull into delta log to track state
if __name__ == '__main__':
    
    DBX_TOKEN = os.environ.get("DBX_TOKEN")
    
    ## Assume running in a Databricks notebook
    dbutils.widgets.dropdown("Query History Lookback Period (days)", defaultValue="3",choices=["1","3","7","14","30","60","90"])
    dbutils.widgets.text("SQL Warehouse Ids (csv list)", "")
    dbutils.widgets.text("Workspace DNS:", "")
    
    lookbackPeriod = int(dbutils.widgets.get("Query History Lookback Period (days)"))
    warehouseIdsList = [i.strip() for i in dbutils.widgets.get("SQL Warehouse Ids (csv list)").split(",")]
    workspaceName = dbutils.widgets.get("Workspace DNS:").strip()
    warehouse_ids = dbutils.widgets.get("SQL Warehouse Ids (csv list)")
    print(f"Loading Query Profile to delta from workspace: {workspaceName} from Warehouse Ids: {warehouseIdsList} for the last {lookbackPeriod} days...")
    
    ## Initialize Profiler
    query_profiler = QueryProfiler(workspaceName, warehouseIdsList)
    
    ## Build Profile
    
    query_profiler.build_query_history_profile( dbx_token = DBX_TOKEN, mode='auto', lookback_period_days=lookbackPeriod)
    
    
