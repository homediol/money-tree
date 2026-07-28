"""
Shared service container
========================

Holds singleton instances of every long-lived service.  Created at
application startup so that request handlers can access them through
dependency injection without re-initialising.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from src.data import DataCollector, Preprocessor, FeatureStore
from src.features.engineering import FeatureEngineeringPipeline
from src.monitoring.drift_detector import DriftDetectionService
from src.monitoring.health import HealthMonitor
from src.monitoring.performance_tracker import PerformanceTracker
from src.pipeline.continuous_learning import ContinuousLearningOrchestrator
from src.prediction.service import PredictionService
from src.registry.dataset_registry import DatasetRegistry
from src.registry.model_registry import ModelRegistry
from src.training.trainer import Trainer
from src.evaluation.reporting import ReportGenerator


@dataclass
class ServiceContainer:
    collector: DataCollector
    preprocessor: Preprocessor
    feature_pipeline: FeatureEngineeringPipeline
    feature_store: FeatureStore
    dataset_registry: DatasetRegistry
    model_registry: ModelRegistry
    trainer: Trainer
    prediction_service: PredictionService
    performance_tracker: PerformanceTracker
    drift_service: DriftDetectionService
    health: HealthMonitor
    continuous_learning: ContinuousLearningOrchestrator
    report_generator: ReportGenerator


def build_services() -> ServiceContainer:
    collector = DataCollector()
    preprocessor = Preprocessor()
    feature_pipeline = FeatureEngineeringPipeline()
    feature_store = FeatureStore()
    dataset_registry = DatasetRegistry()
    model_registry = ModelRegistry()
    trainer = Trainer(
        collector=collector,
        preprocessor=preprocessor,
        feature_pipeline=feature_pipeline,
        feature_store=feature_store,
        model_registry=model_registry,
    )
    performance_tracker = PerformanceTracker()
    drift_service = DriftDetectionService()
    health = HealthMonitor()
    prediction_service = PredictionService(
        collector=collector,
        preprocessor=preprocessor,
        feature_pipeline=feature_pipeline,
        registry=model_registry,
        tracker=performance_tracker,
        drift=drift_service,
        health=health,
    )
    continuous_learning = ContinuousLearningOrchestrator(
        collector=collector,
        trainer=trainer,
        registry=model_registry,
        drift_service=drift_service,
    )
    report_generator = ReportGenerator()

    return ServiceContainer(
        collector=collector,
        preprocessor=preprocessor,
        feature_pipeline=feature_pipeline,
        feature_store=feature_store,
        dataset_registry=dataset_registry,
        model_registry=model_registry,
        trainer=trainer,
        prediction_service=prediction_service,
        performance_tracker=performance_tracker,
        drift_service=drift_service,
        health=health,
        continuous_learning=continuous_learning,
        report_generator=report_generator,
    )
