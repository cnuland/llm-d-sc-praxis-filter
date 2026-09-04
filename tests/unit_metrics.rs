//! Behavior-level tests for the metrics contract.

use std::sync::{Arc, Mutex};

use llm_d_sc_praxis_filter::metrics::{
    CLASSIFY_ATTEMPT_TOTAL, CLASSIFY_DURATION_SECONDS, CLASSIFY_TOTAL, FALLBACK_TOTAL, ROUTE_TOTAL, record_attempt,
    record_classify, record_fallback,
};
use metrics::{
    Counter, CounterFn, Gauge, GaugeFn, Histogram, HistogramFn, Key, KeyName, Metadata, Recorder, SharedString, Unit,
};

#[derive(Clone, Debug, PartialEq)]
enum Event {
    Counter {
        name: String,
        labels: Vec<(String, String)>,
        value: u64,
    },
    Histogram {
        name: String,
        labels: Vec<(String, String)>,
        value: f64,
    },
}

#[derive(Clone, Default)]
struct TestRecorder {
    events: Arc<Mutex<Vec<Event>>>,
}

impl TestRecorder {
    fn events(&self) -> Vec<Event> {
        self.events.lock().expect("test recorder lock poisoned").clone()
    }
}

#[derive(Clone)]
struct TestHandle {
    key: Key,
    events: Arc<Mutex<Vec<Event>>>,
}

impl TestHandle {
    fn labels(&self) -> Vec<(String, String)> {
        self.key
            .labels()
            .map(|label| (label.key().to_owned(), label.value().to_owned()))
            .collect()
    }
}

impl CounterFn for TestHandle {
    fn increment(&self, value: u64) {
        self.events
            .lock()
            .expect("test recorder lock poisoned")
            .push(Event::Counter {
                name: self.key.name().to_owned(),
                labels: self.labels(),
                value,
            });
    }

    fn absolute(&self, value: u64) {
        CounterFn::increment(self, value);
    }
}

impl GaugeFn for TestHandle {
    fn increment(&self, _value: f64) {}
    fn decrement(&self, _value: f64) {}
    fn set(&self, _value: f64) {}
}

impl HistogramFn for TestHandle {
    fn record(&self, value: f64) {
        self.events
            .lock()
            .expect("test recorder lock poisoned")
            .push(Event::Histogram {
                name: self.key.name().to_owned(),
                labels: self.labels(),
                value,
            });
    }
}

impl Recorder for TestRecorder {
    fn describe_counter(&self, _key: KeyName, _unit: Option<Unit>, _description: SharedString) {}
    fn describe_gauge(&self, _key: KeyName, _unit: Option<Unit>, _description: SharedString) {}
    fn describe_histogram(&self, _key: KeyName, _unit: Option<Unit>, _description: SharedString) {}

    fn register_counter(&self, key: &Key, _metadata: &Metadata<'_>) -> Counter {
        Counter::from_arc(Arc::new(TestHandle {
            key: key.clone(),
            events: Arc::clone(&self.events),
        }))
    }

    fn register_gauge(&self, key: &Key, _metadata: &Metadata<'_>) -> Gauge {
        Gauge::from_arc(Arc::new(TestHandle {
            key: key.clone(),
            events: Arc::clone(&self.events),
        }))
    }

    fn register_histogram(&self, key: &Key, _metadata: &Metadata<'_>) -> Histogram {
        Histogram::from_arc(Arc::new(TestHandle {
            key: key.clone(),
            events: Arc::clone(&self.events),
        }))
    }
}

fn with_recorder(test: impl FnOnce(&TestRecorder)) {
    let recorder = TestRecorder::default();
    metrics::with_local_recorder(&recorder, || test(&recorder));
}

#[test]
fn attempt_counts_only_rpc_starts() {
    with_recorder(|recorder| {
        record_attempt();
        record_attempt();
        assert_eq!(
            recorder
                .events()
                .iter()
                .filter(|event| matches!(event,
                Event::Counter { name, .. } if name == CLASSIFY_ATTEMPT_TOTAL))
                .count(),
            2
        );
    });
}

#[test]
fn skipped_classification_has_status_but_no_latency_sample() {
    with_recorder(|recorder| {
        record_classify("SKIPPED_NO_PROMPT", None);
        let events = recorder.events();
        assert!(events.iter().any(|event| matches!(event,
            Event::Counter { name, labels, value }
                if name == CLASSIFY_TOTAL
                    && labels == &[("status".to_owned(), "SKIPPED_NO_PROMPT".to_owned())]
                    && *value == 1)));
        assert!(!events.iter().any(|event| matches!(event,
            Event::Histogram { name, .. } if name == CLASSIFY_DURATION_SECONDS)));
    });
}

#[test]
fn attempted_classification_records_duration() {
    with_recorder(|recorder| {
        record_classify("TIMEOUT", Some(0.125));
        assert!(recorder.events().iter().any(|event| matches!(event,
            Event::Histogram { name, labels, value }
                if name == CLASSIFY_DURATION_SECONDS && labels.is_empty() && *value == 0.125)));
    });
}

#[test]
fn fallback_records_fail_open_only_and_keeps_status_label_bounded() {
    with_recorder(|recorder| {
        record_fallback("TIMEOUT", true);
        record_fallback("RESOURCE_EXHAUSTED", false);
        let events = recorder.events();
        assert!(events.iter().any(|event| matches!(event,
            Event::Counter { name, labels, value }
                if name == FALLBACK_TOTAL
                    && labels == &[("status".to_owned(), "TIMEOUT".to_owned())]
                    && *value == 1)));
        assert!(!events.iter().any(|event| matches!(event,
            Event::Counter { name, labels, .. }
                if name == FALLBACK_TOTAL
                    && labels == &[("status".to_owned(), "RESOURCE_EXHAUSTED".to_owned())])));
    });
}

#[test]
fn route_metric_name_remains_plugin_specific() {
    assert_eq!(ROUTE_TOTAL, "llm_d_sc_route_total");
}
