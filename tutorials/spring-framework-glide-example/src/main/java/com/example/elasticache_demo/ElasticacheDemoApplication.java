package com.example.elasticache_demo;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.cache.annotation.EnableCaching;
import org.springframework.beans.factory.annotation.Autowired;

@SpringBootApplication
@EnableCaching
public class ElasticacheDemoApplication {

	public static void main(String[] args) {
		SpringApplication.run(ElasticacheDemoApplication.class, args);
	}

}
