from dify_plugin import DifyPluginEnv, Plugin

# A Membase search can wait up to a minute while a quiet memory wakes, and the SDK
# allows 90 s per attempt; 300 s covers one retry on top of that.
plugin = Plugin(DifyPluginEnv(MAX_REQUEST_TIMEOUT=300))

if __name__ == "__main__":
    plugin.run()
