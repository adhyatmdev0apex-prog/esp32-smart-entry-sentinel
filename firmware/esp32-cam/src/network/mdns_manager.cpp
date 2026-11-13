#include "network/mdns_manager.h"
#include "app_config.h"
#include "core/logger.h"
#include <ESPmDNS.h>

void initMDNS() {
  if (MDNS.begin(CFG_MDNS_HOSTNAME)) {
    MDNS.addService("http", "tcp", CFG_HTTP_PORT);
    logInfo("MDNS", "Responder started: http://%s.local", CFG_MDNS_HOSTNAME);
  } else {
    logWarn("MDNS", "Failed to start mDNS responder");
  }
}
