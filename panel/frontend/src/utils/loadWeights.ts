// Вес на лету меняют только динамические алгоритмы HAProxy: static-rr принимает
// лишь 0 и 100%, а hash-алгоритмы при смене веса перекидывают уже распределённых клиентов
export const DYNAMIC_ALGORITHMS: ReadonlySet<string> = new Set(['roundrobin', 'leastconn', 'random'])
