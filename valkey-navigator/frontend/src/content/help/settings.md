# Settings

The Settings dialog allows for configuring basic application variables. They are mainly about
* **Docker Endpoint** - To customize where the Docker container runs.
* **InfluxDB settings** - Where historical data is being ingested

**Note**: It is recommended to edit and save the settings the first time you use ValkeyNavigator, even if you do not change anything. As this initializes the connections.

The Settings dialog can be opened by clicking the Gearbox on the top left in the header

![Edit Settings](/help/SettingsEdit.png)

Normally when running the docker containers on your local machine, the Docker Endpoint should be **localhost** but can be set to any host

The **Populate from Session** button will use the Docker Endpoint from where the front end runs.

![Edit Settings Details](/help/SettingsDetails.png)

# Historical Metrics

For recording historical metrics, ValkeyNavigator uses am InfluxDB V2.x time series database. This can be :
* [Amazon Timestream for InfluxDB V2 instance](https://docs.aws.amazon.com/timestream/latest/developerguide/timestream-for-influxdb.html)
* Self manged InfluxDB V2 instance in your own environment

Metrics from clusters are currently written to InfluxDB every 15 seconds. Given for this volume the following sizing is initially recommended in a single AZ deployment:

* Instance db.influx.large:         $170.00
* Storage 100 GB:                   $15.00     
* **Total**  **$185.00**   

Please refer to InfluxDB documentation for how to create the Token and bucket. Valkey Navigator will work also without InfluxDB, but then you will not have historical data in the monitoring page.