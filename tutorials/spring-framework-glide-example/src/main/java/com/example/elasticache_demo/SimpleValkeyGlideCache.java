package com.example.elasticache_demo;

import org.springframework.cache.Cache;
import org.springframework.cache.support.SimpleValueWrapper;
import glide.api.GlideClient;
import glide.api.models.configuration.NodeAddress;
import glide.api.models.configuration.GlideClientConfiguration;
import glide.api.models.commands.SetOptions;
import glide.api.models.commands.SetOptions.SetOptionsBuilder;
import glide.api.models.commands.scan.ScanOptions;
import java.util.concurrent.Callable;
import java.util.concurrent.ExecutionException;

import java.io.*;

import java.util.Base64;
import java.time.Duration;

import glide.api.models.GlideString;
import static glide.api.models.GlideString.gs;

/*
 * A simple Valkey Glide implementation to be able to handle any cache entry, as long as the key has a 
 * toString() method, and the value implements java.io.Serializable
 */
public class SimpleValkeyGlideCache implements Cache {

    private String name;
    private Duration ttl;
    private GlideClient client;

    public SimpleValkeyGlideCache(String name, GlideClientConfiguration config, Duration ttl) {
        try {
            this.name = name;
            this.ttl = ttl;
            client = GlideClient.createClient(config).get();      
        } catch (Exception e) {
            throw new RuntimeException("Failed to create cache " + name, e);
        }
    }

    private String buildKey(Object key) {
        // prefix the cache name to the key in order to avoid clashes
        return name + "::" + key.toString();
    }

    // this works for objects that implement java.io.Serializable
    private String writeValue(Object value) throws java.io.IOException {
        if (value == null) {
            return "";
        }

        ByteArrayOutputStream serialObj = new ByteArrayOutputStream();
        ObjectOutputStream objStream = new ObjectOutputStream(serialObj);
        objStream.writeObject(value);
        objStream.close();

        return Base64.getEncoder().encodeToString(serialObj.toByteArray());
    }

    // this works for objects that implement java.io.Serializable
    private Object readValue(String value) throws java.io.IOException, java.lang.ClassNotFoundException {
        if (value == null || value.length() == 0) return null;

        ByteArrayInputStream serialObj = new ByteArrayInputStream(Base64.getDecoder().decode((value)));
        ObjectInputStream objStream = new ObjectInputStream(serialObj);

        return objStream.readObject();
    }

    @Override
    public String getName() {
        return this.name;
    }

    @Override
    public Object getNativeCache() {
        return this.client;
    }

    @Override
    public ValueWrapper get(Object key) {   
        if (key == null) {
            return null;
        }
        
        try {
            Object value = readValue(client.get(buildKey(key)).get());
            return (value != null ? new SimpleValueWrapper(value) : null);
        } catch (Exception e) {
            throw new RuntimeException("Failed to get value from cache", e);
        }
    }

    @Override
    public <T> T get(Object key, Class<T> type) {     
        ValueWrapper wrapper = get(key);
        if (wrapper == null) {
            return null;
        }
        
        Object value = wrapper.get();
        if (value != null && type != null && !type.isInstance(value)) {
            throw new IllegalStateException("Cached value is not of required type [" + type.getName() + "]: " + value);
        }
        return (T) value;
    }

    @Override
    public <T> T get(Object key, Callable<T> valueLoader) {
        ValueWrapper wrapper = get(key);
        if (wrapper != null) {
            return (T) wrapper.get();
        }
        
        T value;
        try {
            value = valueLoader.call();
        } catch (Exception ex) {
            throw new ValueRetrievalException(key, valueLoader, ex);
        }
        
        put(key, value);
        return value;
    }

    private void setTTL(Object key) throws java.lang.Exception {
        if ( ttl != null ) {
            client.expireAt(buildKey(key), System.currentTimeMillis() + ttl.toMillis()).get();
        }
    }

    @Override
    public void put(Object key, Object value) {
        putWithOptions(key, value, SetOptions.builder());
    }

    @Override
    public Cache.ValueWrapper putIfAbsent(Object key, Object value) {
        ValueWrapper existingValue = get(key);
        if (existingValue == null) {
            putWithOptions(key, value, SetOptions.builder().conditionalSet(SetOptions.ConditionalSet.ONLY_IF_DOES_NOT_EXIST));
        }
        return existingValue;
    }

    private void putWithOptions(Object key, Object value, SetOptionsBuilder options) {
        if ( key == null ) {
            return;
        }

        try {
            if ( this.ttl != null ) {
                options.expiry(SetOptions.Expiry.Milliseconds(System.currentTimeMillis() + ttl.toMillis()));
            }

            client.set(buildKey(key), writeValue(value), options.build()).get();
        } catch (Exception e) {
            throw new RuntimeException("Failed to put value in cache", e);
        }       
    }

    @Override
    public void evict(Object key) {
        evictIfPresent(buildKey(key));
    }

    @Override
    public boolean evictIfPresent(Object key) {
        if ( key != null ) {
            try {
                return !client.getdel(buildKey(key)).get().equals(null);
            } catch (Exception e) {
                throw new RuntimeException("Failed to evict key from cache", e);
            }
        }
        return false;
    }

    @Override
    public void clear() {
        try {
            // we could scan and unlink, but for simplicity just call invalidate
            invalidate();
        } catch (Exception e) {
            throw new RuntimeException("Failed to clear cache", e);
        }
    }

    @Override
    public boolean invalidate() {
        long count = 0;

        try {
            GlideString cursor = gs("0");
            Object[] result;
            ScanOptions options = ScanOptions.builder().matchPattern(name + "::*").build();
            
            do {
                result = client.scan(cursor, options).get();
                cursor = gs(result[0].toString());
                Object[] keys = (Object[]) result[1];

                String[] stringKeys = java.util.Arrays.stream(keys).map(obj -> obj.toString()).collect(java.util.stream.Collectors.joining(", ")).split(", ");
                count += stringKeys.length;

                client.del(stringKeys).get();
            } while (!cursor.equals(gs("0")));

            return count > 0;
        } catch (Exception e) {
            throw new RuntimeException("Failed to invalidate cache", e);
        }
    }
}