# Max number of items we can ask for in ETH requests. These are the values used
# in geth and if we ask for more than this the peers will disconnect from us.
MAX_STATE_FETCH = 384
MAX_BODIES_FETCH = 64 # halved
MAX_RECEIPTS_FETCH = 128 # halved
MAX_HEADERS_FETCH = 192
