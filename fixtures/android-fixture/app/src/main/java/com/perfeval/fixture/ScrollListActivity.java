package com.perfeval.fixture;

import android.app.Activity;
import android.os.Bundle;
import android.util.Log;
import android.view.View;
import android.view.ViewGroup;
import android.widget.ArrayAdapter;
import android.widget.ListView;
import android.widget.TextView;
import java.util.ArrayList;
import java.util.List;

public final class ScrollListActivity extends Activity {

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        int jankBlockMs = getIntent().getIntExtra("jank_block_ms", 0);
        int jankCount = getIntent().getIntExtra("jank_count", 0);
        // 2026-11-06 体检 fixture-sustained-jank：jank_duration_ms > 0 走持续注入
        //（与 MainActivity 同语义，优先于一次性 jank_count）。
        int jankDurationMs = getIntent().getIntExtra("jank_duration_ms", 0);
        int itemCount = getIntent().getIntExtra("item_count", 100);

        List<String> items = new ArrayList<>();
        for (int i = 0; i < itemCount; i++) {
            items.add("Item " + (i + 1));
        }

        ListView listView = new ListView(this);
        ArrayAdapter<String> adapter = new ArrayAdapter<String>(
            this, android.R.layout.simple_list_item_1, items) {
            @Override
            public View getView(int position, View convertView, ViewGroup parent) {
                TextView view = (TextView) super.getView(position, convertView, parent);
                view.setPadding(32, 48, 32, 48);
                view.setTextSize(18);
                return view;
            }
        };
        listView.setAdapter(adapter);
        setContentView(listView);

        Log.i("PerfEval", "PERFEVAL_EVENT {\"type\":\"metric\",\"name\":\"scroll.list_ready\",\"value\":"
                + itemCount + ",\"unit\":\"count\"}");

        if (jankDurationMs > 0) {
            JankInjector.scheduleSustained(listView, jankBlockMs, jankDurationMs);
        } else {
            JankInjector.scheduleCount(jankBlockMs, jankCount);
        }
    }
}
