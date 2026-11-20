/*
 * Reference values for tests/test_cdc_values.py: the Debezium 3.0.6 code paths
 * cdc_values.py reproduces, copied here and run on a JVM with the libraries
 * Debezium 3.0.6 ships (pgjdbc 42.6.1, jackson-core 2.16.2). Not part of the
 * build; run it by hand when a format rule changes:
 *
 *   javac -cp postgresql-42.6.1.jar:jackson-core-2.16.2.jar DebeziumRef.java
 *   printf 'interval\t1 mon\nhstore\t"b"=>"2", "a"=>"1"\n' |
 *     java -cp .:postgresql-42.6.1.jar:jackson-core-2.16.2.jar DebeziumRef
 *
 * One case per line: <kind>\t<value>[\t<width>], kinds hstore, hstore64 (value
 * and answer base64), interval, money, bits, tstz (ISO value, fraction width),
 * dto (SQL Server datetimeoffset as ISO), ulong.
 */
import java.io.*;
import java.math.BigDecimal;
import java.nio.charset.StandardCharsets;
import java.time.*;
import java.time.format.*;
import java.time.temporal.ChronoField;
import java.util.*;
import com.fasterxml.jackson.core.*;
import org.postgresql.util.*;

/** Reference outputs, copied from the Debezium 3.0.6 code paths cdc_values.py follows. */
public class DebeziumRef {
  static final JsonFactory jsonFactory = new JsonFactory();

  // PostgresValueConverter.convertMapToJsonStringRepresentation
  static String hstore(String text) throws Exception {
    Map<String, String> map = HStoreConverter.fromString(text);
    StringWriter writer = new StringWriter();
    try (JsonGenerator g = jsonFactory.createGenerator(writer)) {
      g.writeStartObject();
      for (Map.Entry<String, String> e : map.entrySet()) g.writeStringField(e.getKey(), e.getValue());
      g.writeEndObject(); g.flush();
      return writer.getBuffer().toString();
    }
  }
  // MicroDuration.durationMicros with PGInterval, DAYS_PER_MONTH_AVG
  static String interval(String text) throws Exception {
    PGInterval i = new PGInterval(text);
    double daysPerMonthAvg = 365.25 / 12.0d;
    double numberOfDays = ((i.getYears() * 12) + i.getMonths()) * daysPerMonthAvg + i.getDays();
    double numberOfSeconds = (((numberOfDays * 24 + i.getHours()) * 60) + i.getMinutes()) * 60 + i.getSeconds();
    return String.valueOf((long) (numberOfSeconds * 1e6 + 0));
  }
  // AbstractColumnValue.asMoney + removeCurrencySymbol, then toPlainString
  static String removeCurrencySymbol(String currency) {
    boolean negative = (currency.charAt(0) == '(');
    String s1 = PGtokenizer.removePara(currency).substring(1);
    int pos = s1.indexOf(',');
    while (pos != -1) { s1 = s1.substring(0, pos) + s1.substring(pos + 1); pos = s1.indexOf(','); }
    return negative ? "-" + s1 : s1;
  }
  static String money(String value) {
    BigDecimal d = value.startsWith("-") ? new BigDecimal(removeCurrencySymbol("(" + value.substring(1) + ")"))
                                         : new BigDecimal(removeCurrencySymbol(value));
    return d.toPlainString();
  }
  // PostgresValueConverter.convertBits (String) -> BitSet.toByteArray, base64
  static String bits(String dataStr) {
    BitSet bitset = new BitSet(dataStr.length());
    int len = dataStr.length();
    for (int i = len - 1; i >= 0; i--) if (dataStr.charAt(i) == '1') bitset.set(len - i - 1);
    return Base64.getEncoder().encodeToString(bitset.toByteArray());
  }
  // ZonedTimestamp.getDateTimeFormatter(width) on a ZonedDateTime at UTC
  static DateTimeFormatter fmt(Integer w) {
    if (w == null || w <= 0 || w > 9) return DateTimeFormatter.ISO_OFFSET_DATE_TIME;
    DateTimeFormatter t = new DateTimeFormatterBuilder().appendValue(ChronoField.HOUR_OF_DAY, 2).appendLiteral(':')
      .appendValue(ChronoField.MINUTE_OF_HOUR, 2).optionalStart().appendLiteral(':').appendValue(ChronoField.SECOND_OF_MINUTE, 2)
      .optionalStart().appendFraction(ChronoField.NANO_OF_SECOND, w, w, true).toFormatter(Locale.ENGLISH);
    DateTimeFormatter dt = new DateTimeFormatterBuilder().parseCaseInsensitive().append(DateTimeFormatter.ISO_LOCAL_DATE)
      .appendLiteral('T').append(t).toFormatter(Locale.ENGLISH);
    return new DateTimeFormatterBuilder().parseCaseInsensitive().append(dt).parseLenient().appendOffsetId().parseStrict().toFormatter(Locale.ENGLISH);
  }
  static String tstz(String iso, String width) {
    OffsetDateTime o = OffsetDateTime.parse(iso).withOffsetSameInstant(ZoneOffset.UTC);
    return o.toZonedDateTime().format(fmt(Integer.valueOf(width)));
  }
  // SQL Server datetimeoffset: an OffsetDateTime at its own offset, ISO_OFFSET_DATE_TIME
  static String dto(String iso) { return OffsetDateTime.parse(iso).format(DateTimeFormatter.ISO_OFFSET_DATE_TIME); }

  public static void main(String[] a) throws Exception {
    BufferedReader in = new BufferedReader(new InputStreamReader(System.in, StandardCharsets.UTF_8));
    PrintStream out = new PrintStream(System.out, true, "UTF-8");
    String line;
    while ((line = in.readLine()) != null) {
      String[] p = line.split("\t", 3);
      String r;
      try {
        switch (p[0]) {
          case "hstore": r = hstore(p[1]); break;
          case "hstore64": r = Base64.getEncoder().encodeToString(hstore(new String(Base64.getDecoder().decode(p[1]), StandardCharsets.UTF_8)).getBytes(StandardCharsets.UTF_8)); break;
          case "interval": r = interval(p[1]); break;
          case "money": r = money(p[1]); break;
          case "bits": r = bits(p[1]); break;
          case "tstz": r = tstz(p[1], p[2]); break;
          case "dto": r = dto(p[1]); break;
          case "ulong": r = String.valueOf(new java.math.BigInteger(p[1]).longValue()); break;
          default: r = "?";
        }
      } catch (Exception e) { r = "ERROR " + e.getClass().getSimpleName(); }
      out.println(r);
    }
  }
}
