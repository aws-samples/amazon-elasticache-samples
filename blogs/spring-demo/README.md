# Integrate your Spring Boot application with Amazon ElastiCache

This repository provides the demo code for the blog post https://aws.amazon.com/blogs/database/integrate-your-spring-boot-application-with-amazon-elasticache.

Spring Framework supports transparently implementing caching in an application by providing an abstraction layer. The following code demonstrates a simple example of adding caching to a method by including the `@Cacheable` annotation. Before invoking the `getCacheableValue` method, Spring Framework looks for an entry in a cache named `myTestCache` that matches the `myKey` argument. If an entry is found, the content in the cache is immediately returned to the caller, and the method is not invoked. Otherwise, the method is invoked, and the cache is updated before returning the value.

```
import org.springframework.stereotype.Component;
import org.springframework.cache.annotation.Cacheable;

@Component
public class CacheableComponent {

    @Cacheable("myTestCache")
    public String getCacheableValue(String myKey) {
        // return a value, likely by performing an expensive operation
    }
}
```

Spring Boot provides modules to automatically integrate with a set of providers using convention over configuration. In the following example, adding two module dependencies to the project’s Maven POM file will implement caching:

```
<dependency>
    <groupId>org.springframework.boot</groupId>
    <artifactId>spring-boot-starter-cache</artifactId>
</dependency>
<dependency>
    <groupId>org.springframework.boot</groupId>
    <artifactId>spring-boot-starter-data-redis</artifactId>
</dependency>
```

The `spring-boot-starter-cache` dependency adds basic caching to the application, whilst the `spring-boot-starter-data-redis` adds integration with Redis OSS or Valkey and declares that by default, all caches will exist here.

For configurable values, the Spring Framework application.properties file is updated. In the following example, the endpoint address of a Serverless ElastiCache cache is provided, with all cached entries configured to have a Time-to-Live (TTL) of 10 minutes:

```
spring.data.redis.host=cache1-XXXXX.serverless.euw2.cache.amazonaws.com
spring.cache.redis.time-to-live=10m
```

All Valkey or Redis OSS serverless caches have in-transit encryption enabled. To configure Spring Framework to use in-transit encryption, we add a configuration value to the application.properties file:

```
spring.data.redis.ssl.enabled=true
```

The demo code provided implements this in a simple AWS Command Line Interface (AWS CLI) application. We demonstrate how to build and run this application in the next sections.

## Prerequisites
You will build and run the demo application on an Amazon Elastic Compute Cloud (Amazon EC2) Linux instance, running Linux from AWS. To create an EC2 instance and connect to it using Session Manager, a capability of AWS Systems Manager, refer to Connect to an Amazon EC2 instance by using Session Manager. After you create the instance, note the following information:

•	The IDs of the subnets for the virtual private cloud (VPC) your EC2 instance lives in
•	The ID of the security group assigned to the instance
•	The ID of the EC2 instance

To build the application, you must have the following prerequisites:

•	*Java 17* – To install the Java Development Kit (JDK) 17, run `sudo yum install -y java-17-amazon-corretto-devel` on your EC2 instance
•	*Maven* – To install Apache Maven, run `sudo yum install -y maven` on your EC2 instance

To run the demo application, you also need an ElastiCache cache. We will create this in the next section of this post.

## Create ElastiCache Serverless cache
We use the ElastiCache Serverless option because it allows you to create a cache in under a minute and instantly scale capacity based on application traffic patterns. We begin with the Redis OSS engine, then later upgrade to Valkey to demonstrate that Valkey is a drop-in replacement for Redis OSS with no alterations to the application parameters or code. The demo application will not require any additional changes if you choose to use a self-designed ElastiCache cluster instead of serverless.

To create a serverless cache using the AWS CLI run the following command in AWS CloudShell, replacing `<your VPC subnet IDs>` with a comma separated list of the subnet IDs for the VPC containing your EC2 instance created earlier:

```
aws elasticache create-serverless-cache \
--serverless-cache-name spring-boot-demo \
--engine redis \
--subnet-ids <your VPC subnet IDs>
```

Obtain and note the endpoint address for the cache:

```
aws elasticache describe-serverless-caches \
--serverless-cache-name spring-boot-demo \
--query "ServerlessCaches[0].Endpoint.Address"
```

The cache will have a security group. Obtain and note this security group ID:

```
aws elasticache describe-serverless-caches \
--serverless-cache-name spring-boot-demo \
--query "ServerlessCaches[0].SecurityGroupIds"
```

Your EC2 instance and ElastiCache cache exist in the same VPC. To allow access to the cache from the EC2 instance, you must permit this in the associated ElastiCache security group. To do this, add a rule to the ElastiCache security group permitting access to port 6379 from the EC2 instance security group:

```
aws ec2 authorize-security-group-ingress \
    --group-id <elasticache security group> \
    --protocol tcp \
    --port 6379 \
    --source-group <ec2 instance security group>
```

## Run the demo application

Using your preferred editor on the Linux instance, update the `src/main/resources/application.properties` file to include the endpoint address for the `spring-boot-demo` cache. For example:

```
spring.data.redis.host=spring-boot-demo-XXXXX.serverless.euw2.cache.amazonaws.com
```

Now run the demo application with the following command:

```
mvn spring-boot:run
```

The demo application will build and run. You will see output on the console. An example is shown in the following screenshot. 

 

The output shows that for 100 attempts to invoke the getCacheableValue method, the first was a cache miss, causing the method to be invoked. The following 99 attempts were cache hits, returning the value from the cache without invoking the method. You can run the demo application again and see that there are now 100 cache hits and 0 misses (the cache is still populated from the previous run).

## Upgrade the cache to Valkey
Valkey is an open source, in-memory, high performance, key-value datastore designed to be a drop-in replacement for Redis OSS. Existing Elasticache Redis OSS caches can be upgraded, in-place, to use the Valkey engine by following a simple process.  

The cache will have a major version. Obtain and note this value:

```
aws elasticache describe-serverless-caches \
--serverless-cache-name spring-boot-demo \
--query "ServerlessCaches[0].MajorEngineVersion"
```

Start the upgrade to Valkey:

```
aws elasticache modify-serverless-cache \
--serverless-cache-name spring-boot-demo \
--engine valkey \
--major-engine-version <major engine version>
```

The cache will continue to operate throughout the upgrade. You can run the demo application again with mvn spring-boot:run at any time to see the same results as using the Redis OSS cache.

We can check the status of the upgrade with the following command:

```
aws elasticache describe-serverless-caches --serverless-cache-name spring-boot-demo --query "ServerlessCaches[0].Status"
```

Once the status changes from modifying to available, the upgrade is complete.

Run the demo application one more time with `mvn spring-boot:run`. You will see the same results as using the Redis OSS cache. Valkey is a drop-in replacement for Redis OSS—there are no changes needed to the application code or libraries to use Valkey.

## Cleaning up
To avoid incurring future costs, you can delete the chargeable resources created as part of this post.

Delete the ElastiCache serverless cache:

```
aws elasticache delete-serverless-cache --serverless-cache-name spring-boot-demo
```

Delete the EC2 instance:

```
aws ec2 terminate-instances --instance-ids <your EC2 instance ID>
```

