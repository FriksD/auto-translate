const assert = require('node:assert/strict')
const test = require('node:test')
const fs = require('node:fs')
const path = require('node:path')
const ts = require('typescript')
const vm = require('node:vm')

function loadPythonBridge(httpMock) {
  const filename = path.resolve(__dirname, '..', 'electron/services/python-bridge.ts')
  const source = fs.readFileSync(filename, 'utf8')
  const compiled = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2020,
    },
  }).outputText
  const module = { exports: {} }
  const context = {
    module,
    exports: module.exports,
    require: (name) => {
      if (name === 'node:http') {
        return httpMock
      }
      return require(name)
    },
    Buffer,
    clearTimeout,
    setTimeout,
    __dirname: path.dirname(filename),
    __filename: filename,
  }
  vm.runInNewContext(compiled, context, { filename })
  return module.exports
}

test('diarize request forwards optional speaker count', async () => {
  let capturedBody = ''
  let capturedUrl = ''

  const httpMock = {
    request(url, _options, callback) {
      capturedUrl = url
      const response = {
        statusCode: 200,
        statusMessage: 'OK',
        setEncoding() {},
        on(event, handler) {
          if (event === 'data') {
            handler('{"segments":[]}')
          }
          if (event === 'end') {
            handler()
          }
        },
      }
      const request = {
        on() {},
        write(chunk) {
          capturedBody += chunk
        },
        end() {
          callback(response)
        },
        destroy() {},
      }
      return request
    },
  }

  const { PythonBridge } = loadPythonBridge(httpMock)
  const bridge = new PythonBridge(8000)

  await bridge.diarize('C:/audio/interview.wav', 2)

  assert.equal(capturedUrl, 'http://localhost:8000/api/diarize')
  assert.deepEqual(JSON.parse(capturedBody), {
    audio_path: 'C:/audio/interview.wav',
    num_speakers: 2,
  })
})
