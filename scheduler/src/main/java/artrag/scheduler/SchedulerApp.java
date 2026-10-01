package artrag.scheduler;

import java.io.IOException;
import java.io.InputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Properties;
import java.util.concurrent.Executors;

import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

/**
 * 生產排程服務（Timefold Solver）：只聽本機，給 FastAPI 後端呼叫，不對外開放。
 * <pre>
 *   GET  /health               版本、JVM 記憶體、進行中的求解數
 *   POST /jobs                 送出排程問題（工作分鐘數），回傳 jobId，背景求解
 *   GET  /jobs/{id}?solution=1 目前最佳解的分數、階段、各工序的機台與起訖
 *   POST /jobs/{id}/stop       提前結束，採用目前最佳解
 *   POST /admin/release        記憶體吃緊時清掉舊結果並 GC，把閒置 heap 還給作業系統
 * </pre>
 * 啟動：java -jar target/scheduler.jar [port]（預設 8082；make scheduler）
 */
public final class SchedulerApp {

    private static final ObjectMapper JSON = new ObjectMapper()
            .configure(DeserializationFeature.FAIL_ON_UNKNOWN_PROPERTIES, false);

    private final SolverService solver = new SolverService();
    private final String version = version();

    public static void main(String[] args) throws IOException {
        int port = args.length > 0 ? Integer.parseInt(args[0])
                : Integer.parseInt(System.getenv().getOrDefault("SCHEDULER_PORT", "8082"));
        new SchedulerApp().start(port);
    }

    private static String version() {
        var props = new Properties();
        try (InputStream in = SchedulerApp.class.getResourceAsStream("/scheduler.properties")) {
            if (in != null) {
                props.load(in);
            }
        } catch (IOException ignored) {
            // 版本只用來顯示
        }
        return props.getProperty("timefold.version", "unknown");
    }

    void start(int port) throws IOException {
        var server = HttpServer.create(new InetSocketAddress("127.0.0.1", port), 0);
        server.createContext("/", this::handle);
        server.setExecutor(Executors.newFixedThreadPool(4));
        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            server.stop(0);
            solver.close();
        }));
        server.start();
        System.out.printf("Timefold Solver %s 生產排程服務 http://127.0.0.1:%d（Java %s）%n", version, port,
                Runtime.version());
    }

    private void handle(HttpExchange ex) throws IOException {
        try {
            var path = ex.getRequestURI().getPath().replaceAll("/+$", "");
            var method = ex.getRequestMethod();
            var query = ex.getRequestURI().getQuery();
            if (method.equals("GET") && (path.equals("/health") || path.isEmpty())) {
                send(ex, 200, health());
            } else if (method.equals("POST") && path.equals("/jobs")) {
                var req = JSON.readValue(ex.getRequestBody(), SolverService.ProblemRequest.class);
                send(ex, 201, Map.of("jobId", solver.submit(req)));
            } else if (method.equals("GET") && path.startsWith("/jobs/")) {
                var status = solver.status(path.substring(6), query != null && query.contains("solution=1"));
                if (status == null) {
                    send(ex, 404, Map.of("error", "找不到這個求解工作"));
                } else {
                    send(ex, 200, status);
                }
            } else if (method.equals("POST") && path.startsWith("/jobs/") && path.endsWith("/stop")) {
                var id = path.substring(6, path.length() - 5);
                send(ex, solver.stop(id) ? 200 : 404, Map.of("ok", true));
            } else if (method.equals("POST") && path.equals("/admin/release")) {
                var before = heapMb();
                int removed = solver.release();
                send(ex, 200, Map.of("removedJobs", removed, "before", before, "after", heapMb()));
            } else {
                send(ex, 404, Map.of("error", "not found"));
            }
        } catch (IllegalArgumentException e) {
            send(ex, 400, Map.of("error", String.valueOf(e.getMessage())));
        } catch (Exception e) {
            send(ex, 500, Map.of("error", e.getClass().getSimpleName() + ": " + e.getMessage()));
        } finally {
            ex.close();
        }
    }

    private Map<String, Object> health() {
        var out = new LinkedHashMap<String, Object>();
        out.put("status", "ok");
        out.put("solver", "Timefold Solver");
        out.put("version", version);
        out.put("java", Runtime.version().toString());
        out.put("activeJobs", solver.activeJobs());
        out.put("memory", heapMb());
        return out;
    }

    private static Map<String, Long> heapMb() {
        var rt = Runtime.getRuntime();
        long mb = 1024 * 1024;
        return Map.of("usedMb", (rt.totalMemory() - rt.freeMemory()) / mb, "committedMb", rt.totalMemory() / mb,
                "maxMb", rt.maxMemory() / mb);
    }

    private static void send(HttpExchange ex, int status, Object body) throws IOException {
        byte[] bytes = JSON.writeValueAsString(body).getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(status, bytes.length);
        ex.getResponseBody().write(bytes);
    }
}
