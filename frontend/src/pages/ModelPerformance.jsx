import { useState } from 'react';
import { BrainCircuit } from 'lucide-react';
import PageHeader from '../components/PageHeader.jsx';
import FrozenOpportunityTracker from '../components/FrozenOpportunityTracker.jsx';
import ModelDiagnostics from './ModelDiagnostics.jsx';

export default function ModelPerformance() {
  const [advancedOpen, setAdvancedOpen] = useState(false);
  return <div className="space-y-5">
    <PageHeader eyebrow="Frozen prospective model" title="Models" icon={BrainCircuit}
      description="Track the frozen model’s real 500-round rare opportunity experiment." />
    <FrozenOpportunityTracker advancedOpen={advancedOpen} onAdvancedToggle={setAdvancedOpen}>
      {advancedOpen && <ModelDiagnostics />}
    </FrozenOpportunityTracker>
  </div>;
}
