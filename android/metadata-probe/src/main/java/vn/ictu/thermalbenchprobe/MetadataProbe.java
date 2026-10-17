package vn.ictu.thermalbenchprobe;

import android.app.Activity;
import android.app.Instrumentation;
import android.os.Bundle;
import java.lang.reflect.Method;
import org.json.JSONArray;
import org.json.JSONObject;

/** Read model metadata in the installed target application's classloader; no inference. */
public final class MetadataProbe extends Instrumentation {
    private String modelsJson;

    @Override public void onCreate(Bundle arguments) {
        super.onCreate(arguments);
        modelsJson = arguments.getString("models_json", "[]");
        start();
    }

    private static String error(Throwable value) {
        Throwable cause = value.getCause();
        return (cause == null ? value : cause).toString();
    }

    private static void put(JSONObject output, String key, Object value) throws Exception {
        if (value instanceof Iterable) {
            JSONArray array = new JSONArray();
            for (Object item : (Iterable<?>) value) array.put(String.valueOf(item));
            output.put(key, array);
        } else if (value == null || value instanceof Number || value instanceof Boolean || value instanceof String) {
            output.put(key, value == null ? JSONObject.NULL : value);
        } else {
            output.put(key, value.toString());
        }
    }

    private static void field(JSONObject output, JSONObject errors, Object instance,
                              String key, String method) throws Exception {
        try {
            put(output, key, instance.getClass().getMethod(method).invoke(instance));
        } catch (Throwable failure) {
            errors.put(key, error(failure));
        }
    }

    @Override public void onStart() {
        Bundle bundle = new Bundle();
        JSONObject report = new JSONObject();
        JSONArray observations = new JSONArray();
        try {
            ClassLoader loader = getTargetContext().getClassLoader();
            Class<?> modelInfo = Class.forName("com.google.ai.edge.litertlm.ModelInfo", true, loader);
            JSONArray models = new JSONArray(modelsJson);
            for (int i = 0; i < models.length(); i++) {
                JSONObject model = models.getJSONObject(i);
                JSONObject item = new JSONObject();
                JSONObject errors = new JSONObject();
                Object info = null;
                item.put("model", model.getString("id"));
                try {
                    info = modelInfo.getMethod("from", String.class).invoke(null, model.getString("path"));
                    field(item, errors, info, "max_context_tokens", "maxContextTokens");
                    field(item, errors, info, "dynamic_context", "isDynamicContext");
                    field(item, errors, info, "minimum_runtime_version", "minRuntimeVersion");
                    field(item, errors, info, "model_type", "getModelType");
                    try {
                        Class<?> modality = Class.forName("com.google.ai.edge.litertlm.Modality", true, loader);
                        Object text = modality.getField("TEXT").get(null);
                        for (String method : new String[]{"supportedBackends", "npuBrand", "socName"}) {
                            try {
                                put(item, method, modelInfo.getMethod(method, modality).invoke(info, text));
                            } catch (Throwable failure) { errors.put(method, error(failure)); }
                        }
                    } catch (Throwable failure) { errors.put("modality", error(failure)); }
                    try {
                        Object sampler = info.getClass().getMethod("defaultSamplerParams").invoke(info);
                        JSONObject parameters = new JSONObject();
                        for (String property : new String[]{"TopK", "TopP", "Temperature", "Seed"}) {
                            field(parameters, errors, sampler, "model_default_" + property, "get" + property);
                        }
                        item.put("model_default_sampler_metadata", parameters);
                    } catch (Throwable failure) { errors.put("sampler_metadata", error(failure)); }
                    item.put("status", errors.length() == 0 ? "read" : "read_with_unavailable_fields");
                } catch (Throwable failure) {
                    item.put("status", "metadata_error");
                    item.put("error", error(failure));
                } finally {
                    if (info != null) {
                        try { modelInfo.getMethod("close").invoke(info); }
                        catch (Throwable failure) { errors.put("close", error(failure)); }
                    }
                }
                item.put("field_errors", errors);
                observations.put(item);
            }
            report.put("observations", observations);
            report.put("inference_executed", false);
            report.put("scope", "Metadata declarations, not independent backend execution or realized sampling verification");
            bundle.putString("metadata_json", report.toString());
            finish(Activity.RESULT_OK, bundle);
        } catch (Throwable failure) {
            bundle.putString("probe_error", error(failure));
            finish(Activity.RESULT_CANCELED, bundle);
        }
    }
}
