import UI from '../app/ui.js';

describe('UI file uploads', function () {
    let fetchStub;
    let showStatusStub;

    beforeEach(function () {
        sinon.stub(UI, 'getSetting');
        UI.getSetting.withArgs('file_upload_url')
            .returns(new URL('/', window.location.href).href);
        UI.getSetting.withArgs('file_upload_token').returns('test-token');
        showStatusStub = sinon.stub(UI, 'showStatus');
        fetchStub = sinon.stub(window, 'fetch');
        UI.fileUploadInProgress = false;
    });

    afterEach(function () {
        sinon.restore();
        UI.fileUploadInProgress = false;
    });

    it('sends each dropped file as an authenticated binary POST', async function () {
        fetchStub.resolves({ ok: true });

        const files = [
            new File(['first'], 'first.txt', { type: 'text/plain' }),
            new File(['second'], 'second.txt', { type: 'text/plain' }),
        ];
        await UI.uploadFiles(files);

        expect(fetchStub).to.have.been.calledTwice;
        for (let i = 0; i < files.length; i++) {
            const [url, options] = fetchStub.getCall(i).args;
            expect(url).to.equal(new URL('/api/upload', window.location.href).href);
            expect(options.method).to.equal('POST');
            expect(options.body).to.equal(files[i]);
            expect(options.headers.Authorization).to.equal('Bearer test-token');
            expect(options.headers['X-File-Name']).to.equal(encodeURIComponent(files[i].name));
        }
        expect(showStatusStub).to.have.been.calledWith('Files queued for transfer', 'normal', 5000);
        expect(UI.fileUploadInProgress).to.be.false;
    });

    it('reports an unsuccessful response and stops uploading remaining files', async function () {
        fetchStub.resolves({ ok: false, status: 413, statusText: 'Payload Too Large' });

        const files = [
            new File(['first'], 'first.txt'),
            new File(['second'], 'second.txt'),
        ];
        await UI.uploadFiles(files);

        expect(fetchStub).to.have.been.calledOnce;
        expect(showStatusStub.firstCall.args[0]).to.include('first.txt');
        expect(showStatusStub.firstCall.args[1]).to.equal('error');
        expect(UI.fileUploadInProgress).to.be.false;
    });

    it('rejects unsupported URL schemes', async function () {
        UI.getSetting.withArgs('file_upload_url').returns('ftp://upload.example/file');

        await UI.uploadFiles([new File(['content'], 'file.txt')]);

        expect(fetchStub).to.not.have.been.called;
        expect(showStatusStub).to.have.been.calledWith(
            'File upload URL must use HTTP(S), and HTTPS on secure pages', 'error');
    });

    it('rejects upload URLs with an unsupported path', async function () {
        UI.getSetting.withArgs('file_upload_url').returns('https://relay.example/not-the-relay');

        await UI.uploadFiles([new File(['content'], 'file.txt')]);

        expect(fetchStub).to.not.have.been.called;
        expect(showStatusStub).to.have.been.calledWith(
            'File upload URL must be the relay URL or end in /api/upload', 'error');
    });
});
