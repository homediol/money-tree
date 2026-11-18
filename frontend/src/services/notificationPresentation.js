const presentations = {
  'shadow.created': { category: 'SHADOW RESULTS', title: 'NEW PREDICTION', summary: 'Hashyizweho prediction nshya ya SHADOW.', tone: 'info' },
  'shadow.activated': { category: 'SHADOW RESULTS', title: 'PREDICTION STARTED', summary: 'Session ya SHADOW yatangiye gukora.', tone: 'info' },
  'shadow.win': { category: 'SHADOW RESULTS', title: 'PREDICTION WON', summary: 'Prediction ya SHADOW yatsinze muri iki cyiciro.', tone: 'success' },
  'shadow.loss': { category: 'SHADOW RESULTS', title: 'PREDICTION LOST', summary: 'Prediction ya SHADOW yatsinzwe muri iki cyiciro.', tone: 'danger' },
  'shadow.completed': { category: 'SHADOW RESULTS', title: 'SESSION COMPLETE', summary: 'Session ya SHADOW yararangiye.', tone: 'success' },
  'shadow.stopped': { category: 'SHADOW RESULTS', title: 'PREDICTION STOPPED', summary: 'Session ya SHADOW yahagaze. Impamvu iri munsi.', tone: 'warning' },
  'shadow.paused': { category: 'SHADOW RESULTS', title: 'PREDICTION PAUSED', summary: 'Session ya SHADOW yahagaritswe by’agateganyo.', tone: 'warning' },
  'shadow.alert': { category: 'SHADOW RESULTS', title: 'PREDICTION LIMIT REACHED', summary: 'Prediction yahagaritswe kubera umupaka wagenwe.', tone: 'warning' },
  'rules.improved': { category: 'RULE PERFORMANCE', title: 'RULE PERFORMANCE IMPROVED', summary: 'Imikorere ya rule yarushijeho kuba myiza.', tone: 'success' },
  'rules.warning': { category: 'RULE PERFORMANCE', title: 'RULE WARNING', summary: 'Hari rule isaba kwitabwaho; reba ibisobanuro n’umupaka wayo.', tone: 'warning' },
  'behaviour.detected': { category: 'BEHAVIOUR', title: 'NEW PATTERN DETECTED', summary: 'Sisitemu yabonye imyitwarire mishya mu bisubizo.', tone: 'info' },
  'behaviour.high': { category: 'BEHAVIOUR', title: 'HIGH RESULTS DOMINATE', summary: 'Ibisubizo bya HIGH ni byo birimo kugaragara cyane.', tone: 'info' },
  'behaviour.low': { category: 'BEHAVIOUR', title: 'LOW RESULTS DOMINATE', summary: 'Ibisubizo bya LOW ni byo birimo kugaragara cyane.', tone: 'info' },
  'behaviour.high_rising': { category: 'BEHAVIOUR', title: 'HIGH RESULTS RISING', summary: 'Ibisubizo bya HIGH birimo kwiyongera.', tone: 'info' },
  'behaviour.low_rising': { category: 'BEHAVIOUR', title: 'LOW RESULTS RISING', summary: 'Ibisubizo bya LOW birimo kwiyongera.', tone: 'info' },
  'behaviour.balance': { category: 'BEHAVIOUR', title: 'RESULTS BACK IN BALANCE', summary: 'HIGH na LOW byongeye kugaragara ku rugero rusa.', tone: 'success' },
  'behaviour.transition': { category: 'BEHAVIOUR', title: 'PATTERN CHANGED', summary: 'Imyitwarire y’ibisubizo yarahindutse.', tone: 'info' },
  'behaviour.window_completed': { category: 'BEHAVIOUR', title: '100-ROUND REVIEW COMPLETE', summary: 'Igenzura ry’imikino 100 rirarangiye.', tone: 'success' },
  'behaviour.possible_missing': { category: 'BEHAVIOUR', title: 'ROUND DATA MAY BE MISSING', summary: 'Hari imikino ishobora kubura; genzura niba amakuru yuzuye.', tone: 'warning' },
  'adie.selected': { category: 'ADIE', title: 'NEW WINDOW SELECTED', summary: 'Icyiciro gishya cyatoranyijwe.', tone: 'info' },
  'adie.completed': { category: 'ADIE', title: 'ADIE WINDOW COMPLETE', summary: 'Icyiciro cya ADIE cyarangiye.', tone: 'success' },
  'adie.positive': { category: 'ADIE', title: 'POSITIVE RESULT', summary: 'Ibyavuye mu cyiciro cya ADIE biri hejuru ya zeru.', tone: 'success' },
  'adie.negative': { category: 'ADIE', title: 'NEGATIVE RESULT', summary: 'Ibyavuye mu cyiciro cya ADIE biri munsi ya zeru.', tone: 'danger' },
  'adie.exploratory': { category: 'ADIE', title: 'EXPLORATORY SELECTION', summary: 'Ihitamo ryo kugerageza ryatangiye.', tone: 'info' },
  'adie.integrity': { category: 'ADIE', title: 'ADIE DATA MAY BE STALE', summary: 'ADIE ntiyabonye round nshya yagenzuwe vuba aha.', tone: 'warning' },
  'system.disconnected': { category: 'SYSTEM HEALTH', title: 'LIVE CONNECTION LOST', summary: 'Uhuriro rw’amakuru y’imikino rwarahagaze.', tone: 'danger' },
  'system.stale': { category: 'SYSTEM HEALTH', title: 'GAME DATA IS STALE', summary: 'Nta gisubizo gishya kimaze igihe; genzura Data Watch.', tone: 'warning' },
  'system.restored': { category: 'SYSTEM HEALTH', title: 'LIVE CONNECTION RESTORED', summary: 'Uhuriro rwagarutse; ibisubizo bishya birimo kuza.', tone: 'success' },
  'system.error': { category: 'SYSTEM HEALTH', title: 'SYSTEM ALERT', summary: 'Habaye ikosa; reba ibisobanuro biri mu mateka ya notifications.', tone: 'danger' },
};

export function getNotificationPresentation(event = {}) {
  const base = presentations[event.eventType] || {
    category: 'NOTIFICATION', title: 'NEW NOTIFICATION', summary: 'Hari notification nshya.',
    tone: event.severity === 'critical' ? 'danger' : event.severity === 'warning' ? 'warning' : 'info',
  };
  const direction = String(event.direction || '').toUpperCase();
  if (event.eventType === 'system.error' && event.message) {
    return { ...base, summary: String(event.message) };
  }
  if (event.eventType === 'adie.selected' && direction) {
    return { ...base, title: `${direction} WINDOW SELECTED`, summary: `Icyiciro cya ${direction} cyatoranyijwe.` };
  }
  if (event.eventType === 'adie.exploratory' && direction) {
    return { ...base, summary: `Ihitamo ryo kugerageza rya ${direction} ryatangiye.` };
  }
  return base;
}

export function notificationCategory(event = {}) {
  const category = getNotificationPresentation(event).category;
  if (category.startsWith('SHADOW')) return 'shadow';
  if (category.startsWith('RULE')) return 'rules';
  if (category === 'BEHAVIOUR') return 'behaviour';
  if (category === 'ADIE') return 'adie';
  if (category === 'SYSTEM HEALTH') return 'system';
  return 'other';
}

const sourceRoutes = [
  { prefix: 'shadow.', label: 'SHADOW Predictions', to: '/rules-engine' },
  { prefix: 'rules.', label: 'Rules Performance', to: '/rule-performance' },
  { prefix: 'behaviour.', label: 'Behaviour Detection', to: '/behaviour/overview' },
  { prefix: 'adie.', label: 'ADIE', to: '/dominance-intelligence' },
  { prefix: 'system.', label: 'System Health', to: '/settings' },
];

export function getNotificationSource(event = {}) {
  const type = String(event.eventType || '');
  const route = sourceRoutes.find((item) => type.startsWith(item.prefix));
  return route
    ? { to: route.to, label: route.label }
    : { to: '/notifications', label: event.source || 'Notifications' };
}
