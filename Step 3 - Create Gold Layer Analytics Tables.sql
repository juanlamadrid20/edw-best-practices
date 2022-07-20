-- Databricks notebook source
-- MAGIC %md
-- MAGIC 
-- MAGIC ## Create Gold Layer Tables that aggregate and clean up the data for BI / ML

-- COMMAND ----------

CREATE OR REPLACE VIEW iot_dashboard.hourly_summary_statistics
AS
SELECT user_id,
date_trunc('hour', timestamp) AS HourBucket,
AVG(num_steps) AS AvgNumStepsAcrossDevices,
AVG(calories_burnt) AS AvgCaloriesBurnedAcrossDevices,
AVG(miles_walked) AS AvgMilesWalkedAcrossDevices
FROM iot_dashboard.silver_sensors WHERE user_id = 1
GROUP BY user_id,date_trunc('hour', timestamp)
ORDER BY HourBucket;


CREATE OR REPLACE VIEW iot_dashboard.smoothed_hourly_statistics
AS 
SELECT *,
-- Number of Steps
(avg(`AvgNumStepsAcrossDevices`) OVER (
        ORDER BY `HourBucket`
        ROWS BETWEEN
          4 PRECEDING AND
          CURRENT ROW
      )) ::float AS SmoothedNumSteps4HourMA, -- 4 hour moving average
      
(avg(`AvgNumStepsAcrossDevices`) OVER (
        ORDER BY `HourBucket`
        ROWS BETWEEN
          24 PRECEDING AND
          CURRENT ROW
      ))::float AS SmoothedNumSteps12HourMA --24 hour moving average
,
-- Calories Burned
(avg(`AvgCaloriesBurnedAcrossDevices`) OVER (
        ORDER BY `HourBucket`
        ROWS BETWEEN
          4 PRECEDING AND
          CURRENT ROW
      ))::float AS SmoothedCalsBurned4HourMA, -- 4 hour moving average
      
(avg(`AvgCaloriesBurnedAcrossDevices`) OVER (
        ORDER BY `HourBucket`
        ROWS BETWEEN
          24 PRECEDING AND
          CURRENT ROW
      ))::float AS SmoothedCalsBurned12HourMA --24 hour moving average,
,
-- Miles Walked
(avg(`AvgMilesWalkedAcrossDevices`) OVER (
        ORDER BY `HourBucket`
        ROWS BETWEEN
          4 PRECEDING AND
          CURRENT ROW
      ))::float AS SmoothedMilesWalked4HourMA, -- 4 hour moving average
      
(avg(`AvgMilesWalkedAcrossDevices`) OVER (
        ORDER BY `HourBucket`
        ROWS BETWEEN
          24 PRECEDING AND
          CURRENT ROW
      ))::float AS SmoothedMilesWalked12HourMA --24 hour moving average
FROM iot_dashboard.hourly_summary_statistics
